# Submission readiness: validation contract and evidence ledger

Status: LOCAL IMPLEMENTATION, GATES AND SOURCE REVIEW COMPLETE. Review corrections are implemented;
Lead final repository-script gates passed 906 tests in 10.88s, Ruff clean and
mypy clean on 22 source files. Independent review closed with no remaining source-level defects found. Native Codio and phone acceptance
remain pending. Baseline evidence is separate from new-change evidence.

## Known baseline

Feature 6 reference `397bf94`: Codio Python 3.11.2, 795 passed (20.82s), Ruff clean, mypy clean
on 21 files; one OpenTelemetry deprecation warning. User confirmed voice, two runs
in one process, DONE ignoring non-reset inputs and both narration resets.
PR #6 user-merged as b2d8803; GitHub API and fresh-main content verified.
Unmodified local baseline on that merge: 795 passed in 8.53s, Ruff/mypy21 clean
(Windows Python 3.13.12, real FFmpeg, external network blocked). Earlier Bouncer/Converter phone acceptance
is historical; repeat relevant cases on the final changed build.

## Automated matrix: all external services mocked

| ID | Required coverage | Executed evidence / remaining check |
| --- | --- | --- |
| A01 | Both resets during download, Bouncer, first/next question and summary | Passed: `test_early_workers.py::test_reset_during_early_work_recovers_and_blocks_late_results`, both commands across six boundaries including send |
| A02 | Cancellation-resistant late result at each boundary: no stale send/state/media | Passed in the reset matrix and six `test_early_reset_races.py` cases: Bouncer/question/summary, both commands, timeout and reset cancellations both absorbed |
| A03 | Old cleanup cannot remove a new same-chat portrait; another chat unaffected | Passed: `test_early_intake.py` cancellation-resistant download and replacement-intake cases |
| A04 | Actual-length cap before decode/publication; empty/corrupt bytes rejected | Passed: `test_early_intake.py` admission, metadata-lies, rejected-bytes and mkdir-failure cases plus the actual partial-write regression in `test_readiness_cleanup.py` |
| A05 | Timeout and late return after expiry for acquisition/model/send/notice | Passed worker budgets plus `test_handler_send_deadline.py`: reset/fallback/backstop sends now use the shared 30s budget; returning late success is rejected, cancellation remains cooperative |
| A06 | Active duplicate creates no second worker or future queued answer | Passed: `test_early_workers.py` photo scheduling and duplicate-answer cases; duplicate answers are dropped silently, not queued |
| A07 | Failed question delivery leaves history retryable; retry records once | Passed: `test_failed_question_send_leaves_the_turn_retryable_and_records_once` |
| A08 | Failed question 1 retries without Bouncer; failed summary retains state | Passed: `test_failed_first_question_retries_without_rerunning_the_bouncer` and `test_failed_summary_preserves_the_pending_question_and_prior_answers` |
| A09 | Sixth answer reaches DONE in the same task, one registry.start | Passed: `test_sixth_answer_uses_one_worker_and_one_task_through_every_stage`; that identity test mocks codec calls, separate full-suite tests exercise real FFmpeg |
| A10 | Registered fallback for documents, stickers and voice at every stage | Passed for every real Stage in `test_input_fallback.py`, including BOUNCER_CHECK guidance, GREETING warn-and-ignore, DONE silence |
| A11 | Photo-intake text and interview photo/unknown command guidance | Passed: `test_input_fallback.py::test_text_at_photo_intake_and_wrong_input_at_interview` |
| A12 | DONE inert; later-stage retry rules retained | Full accepted baseline green plus registered unsupported-upload DONE tests; accepted later-stage orchestration bodies unchanged |
| A13 | Shutdown cancellation and pending-work reporting; no unhandled errors | Passed: `test_shutdown_reports_pending_work_without_unhandled_errors`, intake shutdown cleanup and registry backstop tests |
| A14 | Sentinel payloads absent from captured touched-path logs | Passed for tested logging/backstop/intake/cleanup paths; `test_unknown_command_privacy.py` protects three stages after raw-command removal; no SDK-wide claim |
| A15 | Six-answer history/summary contract; supported-facts instruction | Passed: `test_readiness_summary.py::test_summary_receives_all_six_specific_answer_pairs`; prompt unchanged, live quality pending |
| A16 | Baseline assertions preserved within approvals; real codec and SDK wire tests | Post-correction implementer full run: 906 passed, Ruff/mypy22 clean. Lead exact assertion audit passed again; final unchanged repository scripts passed 906 in 10.88s, Ruff/mypy22 clean |

Test paths in this table are relative to [`tests/`](../../tests). Results above
refer to executed local runs (initial integrated and correction runs), not native
Codio or final acceptance.

Use deterministic events and fake clocks/short injected budgets as appropriate,
not sleeps, live requests, or skipped codec tests. Run Python3.11 natively in Codio;
a newer interpreter or semantic replay does not substitute for this gate.

## Rubric-to-evidence map

| Rubric category | Weight | Planned evidence |
| --- | --- | --- |
| Architecture | 15 | MISSION/TECH/ROADMAP, accurate component/state diagrams, ADK role mapping |
| Bouncer | 15 | Exact model, human/non-human tests, phone pet/object rejection, unsupported-upload guidance |
| Interviewer | 20 | Six sequential turns, preserved history, retry integrity, user-reviewed summary depth |
| Converter | 25 | Original bytes + traits in same multimodal request, valid direct photo delivery, phone hybrid quality |
| Scripter/Narrator | 15 | Single paragraph, stored-text Gemini TTS, real codec gates, full phone playback |
| Resilience | 10 | Early/late reset matrix, media directory inspection, wrong-input tests, two runs/no reboot |

Partial-credit numerical values are not specified in the supplied rubric. Do not
invent a numerical grade or claim that test count guarantees full marks.

## Native Codio and phone checklist on final candidate

Record commit, interpreter, actual test count, hooks, codec version and failures.
Keep one bot process; do not include credentials in screenshots.

- [ ] Native scripts/test and scripts/hooks pass with real codec tests unskipped.
- [ ] Text before a photo gets a photo prompt, not a crash.
- [ ] Pet and object photo each rejected warmly; a human selfie advances.
- [ ] Image document and another unsupported upload get appropriate guidance.
- [ ] Six questions appear sequentially; photo/unknown-command input does not count as an answer.
- [ ] Representative output reflects specific supported personality details, not generic labels.
- [ ] Hybrid avatar, one-paragraph narration and complete intelligible voice delivered.
- [ ] DONE ignores non-reset text, photo, command and unsupported media.
- [ ] Two complete runs using restart, same process and no reboot.
- [ ] Both reset commands during Bouncer and Interviewer work return to photo intake.
- [ ] Both reset commands during conversion, scripting and narration prevent new stale sends.
- [ ] Inspect that reset removed this chat's temporary media and did not touch another chat.
- [ ] Fresh run after interruption succeeds without restarting the process.

If a call finishes too quickly to interrupt, record the phone check as unobserved,
not passed. Deterministic offline boundary tests supply race evidence; arrange a
controlled mocked demo if needed without adding production delay controls.

## Final release and submission gate

- [x] Independent review clear, with source review distinguished from test execution.
- [ ] MISSION byte-identical; assertion diff limited to the approved privacy exceptions; no credentials in new trees/history.
- [ ] Final docs link to actual tests/results and accurately describe current implementation.
- [ ] Approved PR description published; user phone acceptance recorded explicitly.
- [ ] User merges final PR; assistant independently verifies merged status/main content.
- [ ] Fresh final-main checkout follows README setup and passes tests/hooks in Codio.
- [ ] Public repository URL and final main commit recorded for submission.

The assistant does not merge, enable auto-merge, install system packages or submit
the coursework. Do not remove old bundles/patches from Codio without separate
permission; keep them untracked and out of the submission commit.

## Local tests-first evidence (not acceptance)

- Unmodified merged `b2d8803` plus the new public-interface regression file:
  **3 failed in 1.75s**, proving the sequential photo callback blocks behind a
  gated Bouncer and document/voice messages have no registered fallback. This
  used an isolated temporary source snapshot, removed after the run. It is
  behavioral RED evidence, not merely missing-symbol collection failures.
- Adapter metadata-only regression: **3 failed, 17 passed** before the fix, then
  **20 passed in 0.04s** after the fix, including sentinel-bearing chat-ID and
  photo-size exceptions.
- Shared backstop sentinel tests: **4 failed, 4 passed in 1.01s** before core logging
  changes, demonstrating raw payload leaks in global error handling, dispatch
  error handling/chat resolution, and the historical photo-download helper.
- Initial early-flow test collection failed because the new module/capabilities
  were absent. This is recorded separately and is not behavioral proof.

These are focused Windows Python 3.13.12 runs with an external socket guard.
The initial integrated gate passed below. Post-review gates and native Codio
acceptance remain pending.

## SDK logging boundary source review

Application metadata-only error checks do not certify SDK logs. The installed
SDK source was inspected, without making model or Telegram requests:

- Google ADK's `models/google_llm.py` builds request/response payload logs at
  DEBUG. Telegram's `_bot.py` logs token-bearing base URLs and request parameters
  at DEBUG. The normal entry point configures INFO, which excludes those DEBUG
  emissions; do not enable global DEBUG when collecting a real interview.
- Google GenAI's `_api_client.py` includes raw aiohttp exception text in INFO
  retry messages. Google ADK's `runners.py` contains ERROR traceback logs for
  root-node cleanup and abort/plugin failure paths. Their source presence is
  confirmed; those exceptional SDK paths were not exercised by this review.
- No SDK patch, blanket exception suppression or whole-process privacy guarantee
  is introduced. Treat live SDK diagnostics as potentially sensitive and do not
  publish them with coursework evidence. Captured synthetic tests only establish
  absence of their sentinels on the paths actually executed.

Inspected local versions: `google-adk 2.11.0`, `google-genai 2.31.0`, `python-telegram-bot 22.8`, `httpx 0.28.1`.


## Initial integrated readiness gate (2026-10-18)

Uncommitted working tree on `feature/2026-10-18-submission-readiness`, based on
`b2d8803`. Lead-executed unchanged `scripts/test -q`: **883 passed in 7.85s**, exit
0. Unchanged `scripts/hooks`: **Ruff clean, mypy clean on 22 source files**, exit 0.
Windows Python **3.13.12**, real portable FFmpeg **8.1.2**, Gemini/Telegram mocked,
external socket guard enabled. Windows shims select the interpreter/tools without
editing repository scripts. No codec skip or deselection. This does **not** replace
the native Python 3.11 Codio gate.

Lead baseline audit against `b2d8803` passed:
- 417 baseline test function bodies unchanged (parameterized cases explain the
  difference from the 795-test baseline count).
- Exactly six leaking assertions replaced across the five approved privacy tests.
- All 26 assertions in the two approved integration fixture adaptations preserved.
- Accepted Converter/Scripter/Narrator orchestration bodies AST-identical.
- MISSION, dependencies, gate scripts and protected model/provider/codec files
  unchanged; `.env` ignored and untracked.
- No files staged or committed. The accidental untracked `uv.lock` and unused
  checkout `.venv` were subsequently removed by their creating implementer; neither
  enters the candidate and dependencies are unchanged.

This gate predates final review corrections. In particular, reset/fallback handler
sends were found to lack the specified application send budget despite worker-send
coverage passing. Final review, correction evidence and rerun must be recorded
before preparing the release candidate.


## Final-review follow-ups

Independent source review confirmed the lead findings: handler-level message
sends lacked the application deadline, and unknown-command warnings included
user-controlled command text. Both have tests-first corrections: **7 failed / 33
passed** before the fixes, then **40 passed in 2.01s**. The full implementer suite
passed **906 in 11.19s**, Ruff clean, mypy clean on 22 source files. A timeout
requests cancellation; it does not forcibly stop a cancellation-resistant dependency.
Do not describe it as an unconditional wall-clock guarantee.

Additional lead-executed filesystem checks in `tests/test_readiness_cleanup.py`:
**4 passed in 0.40s**, Ruff clean. Both reset commands clear state before a simulated
purge `OSError` while logging no sentinel payload. A simulated partial `write_bytes`
failure removes the partial portrait and never calls Bouncer. An unlink error logs
metadata without a traceback. These tests explicitly retain the file when deletion
fails; no successful-cleanup claim is made in that case. An initial new-test fixture
used an illegal stage transition; correcting the setup required no production change.

Eight Markdown files have balanced fences and 24 valid local links/anchors.
Mermaid rendering remains unverified. A scoped scan of the then-current 30 candidate
source/test/doc files found no recognizable credential patterns; this is not proof
against arbitrary secret strings and did not read `.env` contents. Repeat against
the final candidate after review corrections.


The lead re-ran the unchanged assertion audit after the B1/B2 corrections: all
417 protected test bodies, 26 pipeline assertions and accepted later-stage
orchestration checks still pass. Original names of the five approved privacy tests
are retained. Independent closure and final repository-script results are recorded below. No native Codio or phone result is implied.


## Final local repository-script gates (2026-10-18)

Lead ran the unchanged `scripts/test -q` and `scripts/hooks` after the review fixes
and timeout-documentation correction: **906 passed in 10.88s**, exit 0; **Ruff
clean, mypy clean on 22 source files**, exit 0. Same Windows Python 3.13.12,
real FFmpeg 8.1.2, offline socket guard, no live Gemini/Telegram calls, no skipped
codec tests. The accepted 795 cases remain, plus 111 readiness cases.

Photo/voice send primitives remain guarded by the accepted outer orchestration
budgets at every live call site. They are not new unbounded handler sends; no
extra change to accepted later-stage code is required. Native Python 3.11 Codio,
phone acceptance and user merge remain pending.


## Independent review closure (2026-10-18)

A separate reviewer compared the touched application and tests with `b2d8803`,
then re-reviewed the corrections. **No remaining source-level defects found.**
B1 (handler sends), B2 (raw command logging), and D1-D3 (memory/deadline wording
and Bouncer fallback evidence) are closed. The six-case combined expiry/reset
matrix and four cleanup-failure cases were reviewed. Reviewer inspected execution
logs but did not independently execute tests: final gate execution above is by the
lead, not the reviewer. No native Codio or phone acceptance is claimed.

Local review/gates are sufficient to prepare the transfer candidate, not to merge
or submit. Mermaid fences/links are checked; GitHub rendered preview remains a
publication-time check. Prompt quality still requires a representative live run.
