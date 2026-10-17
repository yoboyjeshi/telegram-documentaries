# Feature 6 validation record

## Status

Implementation and final automated verification are complete. Codio phone testing,
publication and merge are pending.
No live Gemini or Telegram result is claimed.

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
