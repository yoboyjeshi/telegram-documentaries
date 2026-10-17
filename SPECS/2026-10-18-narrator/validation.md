# Feature 6 validation record

## Status

Implementation passed the initial Windows automated gates. The first Codio run
exposed a Python 3.11 cancellation defect, documented below with its correction.
The cancellation correction subsequently passed native Codio gates and PR #6 was
published without merging. Phone testing exposed a Scripter schema compatibility
error before narration; see the latest correction below. Voice acceptance and
merge remain pending.

Baseline: merged main `0dec5956c42201538d8c6adb1fa3ef5762d96fe7`.
Branch: `feature/2026-10-18-narrator`.
User approved requirements and implementation on 2026-10-18.

## Baseline and regression integrity

Before implementation, the unchanged repository scripts were run through an
external Windows shell/virtualenv runner with an offline network-blocking guard:

- `scripts/test -q`: **439 passed in 4.95s**.
- `scripts/hooks`: Ruff clean; mypy clean, **18 source files**.
- Local credentials were absent; Gemini and Telegram traffic was mocked.

Fresh AST comparison checks all **285 original top-level test functions**
(439 parameterized baseline cases): no missing or changed test bodies.
The only modified historical test file is `test_scripting_routing.py`: its
application-builder helper now stubs LazyNarrator to None to isolate Feature 5.
Original assertions are retained. Independent review approved that isolation;
Feature 6 separately tests real registered-handler continuation to DONE.
The whole historical routing file passed: **27 passed in 2.00s**.
MISSION.md, gate scripts and project configuration remain byte-identical to main.
The local .env is absent, ignored and untracked; commit trees are checked before
and after each commit. No Codio credentials are copied locally.

## Red/green evidence

- Guarded voice send: **4 expected failures** before implementation, then 4 passed.
- Narrating entry/media path/DONE contract: **3 expected failures** before implementation.
- Voice commit ordering: **2 expected failures** before Narrator integration.
- Provider/format suites initially failed collection because modules did not exist.
  Malformed MIME quote coverage later produced **4 expected failures** before repair.
- Codec cleanup contract initially failed collection on missing drain_codec_cleanup.
  Subsequent process-ownership and termination regressions were added before repairs.
- Application stop-hook integration: **2 failures** observing only pipeline close,
  then **2 passed** after adding codec drain. Two additional cancellation-boundary
  tests passed, proving cancellation propagates without error logging.

The routing slice landed after production implementation and therefore has no
pre-implementation red evidence. Disposable-copy mutation checks produced
**12 failures** for premature DONE, **43** for bypassed normalization and **1**
for removed explicit expiry checks. These are regression-detection evidence,
not a claim of tests-first implementation for that slice.

## Settled focused checks (overlapping, do not add counts)

- Provider/format: **119 passed in 1.97s** (68 format, 51 provider), scoped Ruff
  and mypy clean. Actual google-genai 2.31.0 serialization is tested using
  MockTransport, including the exact model, Charon voice, verbatim stored text
  and one HTTP request on 429/500/503. No real endpoint traffic.
- Narrating step/routing: **103 passed in 2.58s**, Ruff clean. Registered handlers,
  resets, cancellation-resistant returns, replacement ownership and deadlines
  cover generation, encoding, final validation and sending.
- Lead integration subset: **13 passed in 1.25s**, including two full mocked
  registered-handler pipelines from photo through six questions to DONE.
- Three malformed injected encoder artifacts passed rejection tests through the
  actual default validator before a file, send or decode could occur.

Earlier partial assembled counts are not final gate results. They excluded codec
work that was still changing. Final results must include every test, without
skips or deselection of the real codec gates.

## Independent review

Provider/format and orchestration/routing source review found no remaining defect
in those slices. Model and voice are fixed through normal constructor/application
wiring; they are not literally immutable Python attributes. Encoding plus final
decode validation intentionally share the 15-second codec sub-budget.

Codec review identified and drove corrections for tracked process ownership,
terminate-to-kill escalation after a failed wait, strict decoding, and the real
corruption test. That test must first prove its damaged audio still passes
container inspection, then prove the real decoder rejects it. A fresh independent
read confirmed both final findings closed. No unresolved source-review blockers
remain; executable real-codec evidence is still pending.

The frozen codec owner's assembled run reported **785 passed, 3 failed in 8.8s**;
all failures were mandatory real-codec gates because FFmpeg was absent from PATH.
Ruff was clean and mypy passed for 21 source files. This is not the lead's final
unfiltered script gate with a working codec.

## Real codec prerequisite and mandatory gates

FFmpeg needs libopus encoding, OGG mux/demux, Opus decoding and the s16le output
muxer. The original local vendored binary lacked the required muxers. The user
approved a workspace-only portable build; no system install or permanent PATH
change is permitted. Verify the publisher SHA256 before executing it.

The full suite must execute, not skip:
1. Real WAV to OGG/Opus encode and bounded decode.
2. Structurally valid OGG carrying invalid Opus framing rejected by real decode.
3. Real codec through orchestration, valid artifact delivered to mocked Telegram,
   DONE committed only after send, generated audio removed.

Byte checks cannot establish intelligible speech, exact spoken words, requested
voice quality or access to the live model.

## Codio acceptance

Follow plan.md's phone checklist: complete and listen to two full runs in one
process; verify terminal DONE and both reset commands during narration. Record
any interruption too fast to observe rather than claiming a pass. User phone-test
result is required before merge. The assistant does not merge or enable auto-merge.

## Final lead gate: PASS

On the frozen implementation, the unchanged repository scripts ran through the
Windows shell runner, Python 3.13.12, with external networking denied and secret
environment variables removed:

- `sh scripts/test -q`: **788 passed in 11.53s**, zero failures or skips.
- `sh scripts/hooks`: **Ruff clean; mypy clean, 21 source files**.
- All three mandatory real codec gates executed successfully, including actual
  corrupted-packet decode rejection and real-codec orchestration delivery/cleanup.

Used Gyan FFmpeg **8.1.2 essentials**, downloaded from the publisher's GitHub
release mirror and checked against the publisher's independent SHA256 file:
`db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec`.
The executable stayed outside the repository, in the review workspace. PATH was
changed only for the test subprocess, never permanently. No system installation.
This final run supersedes every pending or failed codec gate described above.
No live model/Telegram call or Codio phone-test result is claimed.


## Codio compatibility failure (2026-10-18)

The first Codio run on Python 3.11.2 completed 787 tests successfully and failed
`test_repeated_cancellation_still_reaps_and_never_hangs`: the encoder call had
returned cancellation before the process received kill. The real codec gates
passed. This supersedes any implication that the earlier Windows-only green
run proved Codio compatibility.

The lead independently reproduced the identical assertion failure by replaying
only the reviewed `wait_for`, `_cancel_and_wait` and `_release_waiter` functions
from CPython v3.11.2 on the existing Python 3.13 interpreter. The source was
fetched from the CPython repository, not added to application code. Python 3.11
`wait_for` creates a child task and a second cancellation can interrupt its wait
for that child without waiting for the child cleanup. Python 3.12+ uses a
same-task timeout implementation for positive timeouts.

This reproduction is compatibility-semantic evidence, not a native Python 3.11
or Linux run. The corrected branch still requires Codio tests and hooks, followed
by user phone acceptance. No tests may be skipped or weakened.


### Correction and final local gates

Replaced the three codec timeout wrappers with Python 3.11-supported
`asyncio.timeout` contexts. Codec work and its cleanup now remain in the same
caller task. No cleanup assertion was relaxed: all 44 previously delivered
codec test bodies are AST-identical. Added two regression cases covering encode
and validate with a simulation of the old child-task cancellation behavior.

The unchanged repository scripts on Windows Python 3.13.12 passed after this fix:
- `scripts/test -q`: **790 passed in 10.41s**, zero skips, all real codec gates.
- `scripts/hooks`: Ruff clean, mypy clean on 21 source files.

Original 285 baseline test function bodies remain unchanged. MISSION.md, gate
scripts and configuration remain byte-identical; .env remains absent and ignored.


The full suite also passed with the exact upstream Python 3.11.2 wait_for
functions replayed: **790 passed in 13.98s**, including real FFmpeg. This remains
a semantic replay on Python 3.13, not a native Codio result.


## Phone-test Scripter compatibility correction (2026-10-18)

The user-provided Codio screenshot confirmed the cancellation correction at
97d0db7: Python 3.11.2, 790 passed, Ruff clean and mypy clean on 21 files.
PR #6 is published; no merge or auto-merge is authorized.

The first phone run produced an avatar but failed at Scripter, before TTS.
Gemini rejected additional_properties in the serialized responseSchema. An
HTTP-mocked test using the real ADK and google-genai 2.31 client reproduced that
exact outgoing field before the fix (1 failed, 4 passed). No live API was called
by the assistant.

The minimal correction removes only additionalProperties from the exported
Pydantic JSON schema via json_schema_extra. NarrationScript remains the same
ADK output_schema class; strict typing, extra-field rejection, forced instance
revalidation and paragraph/word limits remain enabled locally. The callback
uses Pydantic's JsonDict annotation. Dependencies, model and prompts are unchanged.

The new wire-level regression asserts the actual serialized request schema and
successful ADK parsing with only HTTP replaced. Four additional cases prove
extra fields, non-string text, excess words and multiline text still fail local
validation. Existing tests/assertions are unchanged. Independent read-only
source review found no blockers; the reviewer did not execute tests.

Final corrected local gates, Windows Python 3.13.12 with networking blocked:
- Unchanged scripts/test -q: **795 passed in 9.45s**, no skips, real FFmpeg included.
- Unchanged scripts/hooks: Ruff clean; mypy clean on 21 source files.
- All 285 original baseline test bodies and all 44 delivered codec test bodies
  remain unchanged. MISSION.md and gate scripts remain byte-identical to main;
  .env remains absent locally, ignored and untracked.

This new correction still requires native Codio gates and phone acceptance.
Restarting the bot loads the fix but clears its in-memory interview state, so
phone testing must start a fresh interview after deployment. No playback success
is claimed. Keep the same PR #6 unmerged, with auto-merge disabled.
