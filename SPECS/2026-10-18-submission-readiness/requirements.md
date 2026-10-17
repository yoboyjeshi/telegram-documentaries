# Submission readiness: requirements

## Status and authority

2026-10-18: BRIJESH DUTT approved the submission-readiness plan and requested specs.
Implementation authorized on 2026-10-18: "I merged PR #6, verify and proceed".
PR #6 verified user-merged as b2d8803b4b203691d5db12b2e47c9ae6e6c8447b; fresh
main tree exactly matches accepted Feature 6. Work is on the separate readiness branch.
Reference: Feature 6 head `397bf941366a3372b573a20bc5c2296456b1c590`.
Known evidence: native Codio Python 3.11.2, 795 tests passed, Ruff/mypy clean on
21 files; user confirmed full voice playback, two runs in one process, terminal
DONE and both resets during narration. These are historical results, not results
for this change. PR #6 is now verified merged; the evidence above remains historical.

Implementation prerequisite: the user merges PR #6. Verify GitHub and the resulting
main tree before creating `feature/2026-10-18-submission-readiness` in a fresh
workspace checkout. A squash merge will have a different SHA; verify content,
not ancestry alone. Preserve the accepted Feature 6 checkout and artifacts.

## Scope and invariants

- One focused hardening/documentation PR; assistant never merges or enables auto-merge.
- Keep MISSION.md byte-identical, model IDs, six-question interview, output
  contracts and accepted audio/codec guarantees. Retain existing tests except
  the five explicit privacy-contract exceptions and two assertion-preserving pipeline
  fixture adaptations below, approved on 2026-10-18. No silent test weakening or compatibility hacks.
- No frontend, database, extra models, dependency upgrades, automatic installation,
  broad refactor, new task registry or globally concurrent Telegram update handlers.
- Credentials stay in Codio .env, ignored and untracked. No live external calls in
  automated tests. Preserve existing ADK and SDK wire-compatibility regressions.
- Direct Gemini TTS remains an integration, not a fifth ADK agent role.
- Supported photo intake remains Telegram photos. Documents are politely rejected,
  not downloaded or silently treated as correctly typed portrait bytes.

## R1: architecture and submission documentation (rubric: 15 points)

README.md, SPECS/TECH.md and SPECS/ROADMAP.md must accurately describe the submitted
version. Add component and state diagrams to TECH, linked from README. Explain:
- Four ADK roles: Bouncer, Interviewer, Converter and Scripter. Interviewer may use
  separate question/summary LlmAgent instances; role count is not instance count.
- ADK executes the model agents. Application code owns stage transitions,
  per-chat interview history and task ownership; no fictitious ADK parent agent.
- Converter receives original portrait bytes and interview traits together in a
  native multimodal image-generation request. It has no image-to-description-to-
  image model chain. Fixed instructions and trait text are still present.
- Direct TTS, FFmpeg OGG/Opus conversion, guarded delivery and cleanup.
- How to clone, configure local secrets, verify codec support, run and test.

Correct stale Feature 6 status without overwriting historical evidence. Record
merge status only after verification. Add a rubric-to-evidence map and distinguish
automated checks, user-reported phone checks and checks still pending. Existing
telegram-arch.html explains polling versus webhooks, not the complete agent
architecture; preserve it, but do not count it as the missing pipeline diagram.

## R2: responsive early-stage reset (rubric: resilience, 10 points)

Extend existing ConversionTasks ownership to photo intake, Bouncer calls and each
Interviewer turn. Keep one active worker per chat and retain retired workers until
completion. Sequential handlers schedule long work and return promptly.
Photo intake and interview callbacks currently await work directly without registry
ownership. This requires new integration at those entry points, not a registry
configuration change; retain the existing single registry and its lifecycle rules.

For both /start and /restart during download, Bouncer, question generation or
summary generation:
1. Invalidate the session before cancelling its worker; do not await model completion.
2. Clear interview/narration state, purge that chat's media and enter AWAITING_PHOTO.
3. Send the photo prompt via bounded delivery. Network delivery is not guaranteed.
4. Late work must not send a new reply, commit state, recreate portrait files or
   overwrite/delete replacement-session artifacts. Cancellation is not an error notice.

Ownership means matching session version and expected stage, no pending task
cancellation, and an unexpired deadline. Recheck after every external await and
before writes, sends and commits. Old references to mutable session objects must
not bypass these checks. All worker exceptions are retrieved and logged safely.

Per-operation budgets: Telegram photo acquisition 30 seconds total; Bouncer 60
seconds; each Interviewer question or summary call 60 seconds; sends and a single
failure notice 30 seconds each. These are application limits, not SDK retry loops.
Use Python 3.11-compatible timeout contexts with explicit expiration checks.
Do not impose a deadline on the user's thinking time. Keep later-stage budgets.
A provider that ignores cancellation forever cannot be forcibly stopped by asyncio:
reset must remain usable and isolate it; shutdown reports pending work honestly.

## R3: portrait ownership and bounded intake

Do not let an asynchronous downloader write to the shared final photo.jpg path:
a cancellation-resistant download could recreate it after reset. Download into
attempt-owned memory using the existing Telegram SDK, then apply a 10 MiB
admission cap before validation or filesystem publication. Precheck advertised
size when available, but recheck actual received length. Publish to the existing
photo_dest only after ownership/deadline checks, with no await between the last
check and publication. Reject empty/oversized/invalid photo data safely;
validate actual JPEG bytes before declaring image/jpeg to the existing agents.
No added dependency or image-format expansion is needed; Pillow already exists.
The installed SDK buffers request.retrieve before writing the supplied sink: this
is NOT a streaming transport-memory cap, even with a capped BytesIO sink. State
that limitation honestly. Do not add a custom HTTP downloader in this scope.

Preserve injected download test seams where safe; live wiring must use the guarded
publication path. Remove partial, rejected and failed-intake artifacts owned by
that attempt. Keep an accepted portrait through downstream generation/retries;
reset purges it. Never have old cleanup remove a newer attempt's same-path file.
Retired downloads own only memory, not a writable replacement-session path.

## R4: turn integrity and handoff (rubric: Interviewer, 20 points)

Keep six accepted answers paired with their questions. Wrong inputs and duplicate
active updates must not create another turn, download or parallel model request.
For production interview delivery, stage candidate state locally: unsuccessful
question delivery leaves the previous accepted conversation retryable. Do not
silently record an answer while leaving the user unaware of the next question.
Use ownership-guarded commit after confirmed send, without another await.

Photo acceptance and question 1 run in the same intake worker. The sixth answer,
summary and completion acknowledgement continue directly into Converter ->
Scripter -> Narrator in that same worker/version; do not call registry.start again
from inside the active worker. The sixth-answer update starts exactly one worker;
its asyncio task object stays identical through summary and every downstream stage.
Reuse the existing tracked convert_step/script nested-await continuation, not the
standalone retry branches that call conversion_tasks.start. Preserve Converter/Scripter/Narrator-only retry
routing. Earlier direct helper test seams may remain, but do not mistake them for
proof of live handler correctness.

Wrong-stage text/photo/unknown commands get appropriate guidance, without being
recorded or queued as answers to a future question. Guidance must not overwrite an
active worker or make resets wait on a provider. DONE remains silent except resets.
After a failed first question, text can retry question 1 without redoing a successful
Bouncer; failure/timeout in intake returns to AWAITING_PHOTO for photo retry.
Failed later questions or summary preserve the pending question and prior answers.

## R5: unsupported input routing (rubric: Bouncer and resilience)

Register a final message fallback behind start/restart, text, photo and unknown
command handlers. Document/sticker/voice uploads must not download media, call a
model, advance state or trigger generation retries. While awaiting a photo, ask
for a normal Telegram photo; during interview ask for a written answer; during
active generation give appropriate bounded guidance or ignore without duplicate
work. DONE ignores them. Unsupported non-message updates are safely ignored.
Test actual Application handler selection, not only normalised-input helpers.

## R6: privacy in touched early-stage paths

No raw exceptions, tracebacks, credentials, portraits, questions, answers or
summaries in new/touched error logging, including decorators and global handlers
reachable from these paths. Use operation/chat/stage/exception type only. Test
sentinel values in provider, downloader, send and cleanup errors. Review log
emissions from SDK/transport boundaries; do not claim whole-repo privacy from
checks of a single logger. Do not introduce blanket exception suppression.

### Approved exception: two historical logging assertions

The original promise to retain every assertion conflicts with metadata-only
privacy in two inspected tests:
1. tests/test_orchestrator.py::test_failure_logs_type_and_message_but_not_answer_text
   requires literal raw exception text "gemini timeout" in the log.
2. tests/test_logging.py::test_guard_errors_logs_and_returns_fallback requires
   a non-empty traceback (record.exc_info).

BRIJESH DUTT explicitly approved on 2026-10-18: "Approve the two privacy assertion changes".
Approved narrow exception: retain their functional assertions,
replace only those leakage requirements with exception-type/operation checks and
absence of raw exception text/tracebacks. Add sentinel payload checks. Record an
exact before/after assertion audit. Keep all other baseline assertions unchanged.
Do not fake matching literal messages, create unsafe test-only helpers or delete
the tests to get green gates. Any additional baseline assertion change requires
separate approval. This approval does not waive the PR #6 user-merge prerequisite.

### Approved additional exception: global error handler regression

Implementation source audit found a third conflicting baseline test missed by
spec review: tests/test_bot.py::test_on_error_logs_the_underlying_exception.
It selects records with exc_info, requires that list to be non-empty, and requires
the stored exception object to be the original error. This mandates a traceback
and raw exception retention in the live global handler, contrary to R6.
BRIJESH DUTT separately approved on 2026-10-18: "Approve the third privacy test change".
Approved additional exception: replace traceback-presence/exception-identity
checks with ERROR-level operation and exception-type checks plus absence of raw
message, exc_info, and exc_text. Retain the test and all unrelated assertions.
Do not create an unsafe compatibility helper to evade this privacy requirement.
All other baseline assertions remain protected; executable acceptance is still pending.

### Approved additional exceptions: adapter and photo-flow logging

After the third approval, the lead enumerated all 1,072 baseline Assert nodes
and reviewed 127 logging-related assertions against merged b2d8803. Two further
conflicts were found and separately approved by BRIJESH DUTT on 2026-10-18
(response: "approved" to the consolidated four-test change request):
- tests/test_adapter.py::test_unusable_chat_id_returns_none_and_logs_debug_detail
  requires a non-null exc_info in debug records. Approved: retain None return and
  debug observability, require exception-type metadata and no traceback/raw input.
- tests/test_photo_flow.py::test_bouncer_failure_logs_no_image_bytes requires raw
  "gemini timeout" in logs. Approved: retain retry/stage/image-privacy checks,
  require error-type/operation metadata and absence of raw message/tracebacks.

No additional positive raw-error/traceback requirements were found in this sweep.
Replace only these leakage requirements; do not disguise unsafe logging as
compatibility code. All unrelated assertions remain protected.

### Approved baseline integration-test fixture adaptations

Two historical integration tests assume synchronous early-stage callbacks and mock
only download_to_drive with non-image bytes:
- tests/test_scripter_handoff.py::test_two_complete_offline_runs_from_portrait_without_reboot
- tests/test_narrator_full_pipeline.py::test_two_registered_handler_pipelines_to_done

Approved bounded adaptation: retain every existing assertion; mock SDK
download_to_memory using valid synthetic JPEG portrait bytes instead of the old
non-image disk-write fixture. Await the existing registry deterministically after
photo intake and each interview answer before asserting results or sending the
next answer. No sleeps, skips, assertion weakening or live disk-download fallback.
BRIJESH DUTT explicitly approved these two fixture/synchronization adaptations
on 2026-10-18 in the same consolidated approval. No other test changes are authorized.

## R7: summary quality and final output (rubric: 20/25/15 points)

Evaluate a synthetic six-answer example before changing prompts. Assess whether
specific supported habits, quirks and motivations survive synthesis. The rubric
requires comprehensiveness, not an arbitrary longer word count. If evidence shows
thin output, refine only summary instructions, preserving TraitSummary and the
no-invented-facts rule. Treat user answers as data, not instructions. Do not add
new model calls or publish private interview transcripts. Automated tests verify
prompt/input/contract behaviour, not real model prose quality; phone review is
required. Keep existing multimodal Converter and one-paragraph/playable-voice flow.

## R8: delivery and acceptance

All baseline and new tests must pass without skip/deselection of real codec tests;
Ruff/mypy clean, independent source review clear, native Codio gates and new phone
stress checks complete. Record actual counts, never predeclare a target count.
Inspect temporary storage as well as chat UI during reset testing. User merges
final PR; verify fresh final-main setup/tests and public repo access before
submission. Historical acceptance does not substitute for testing changed paths.
