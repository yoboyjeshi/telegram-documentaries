# Submission readiness: implementation plan

Status: implementation authorized and started on the separate readiness branch.
PR #6 user-merge and fresh-main content verification complete.
Unmodified local baseline: 795 tests passed in 8.53s, Ruff/mypy clean (21 files),
Windows Python 3.13.12 with real FFmpeg and network guard. Native Codio retest pending.
See requirements.md for contracts, scope and acceptance boundaries.

## Sequence and approval gates

1. User merges Feature 6 PR #6. Read its actual merge status; never merge for them.
2. Fetch fresh main into a new workspace checkout, compare accepted Feature 6
   content, inspect git status/activity, then create the planned feature branch.
   If main has unrelated changes, review differences before assuming this baseline.
3. Run unchanged scripts/test and scripts/hooks offline with real FFmpeg. Record
   interpreter, dependency versions and exact commit. Preserve all 795 reference
   cases; preserve assertions except the five explicitly approved privacy exceptions.
   Two approved pipeline fixture adaptations retain every assertion. All seven narrow
   exceptions are described in requirements.md (user approvals on 2026-10-18).
4. Add failing tests for each slice below before production edits. Record red and
   green evidence, including tests that actually fail for the identified defect.
5. Implement slices, then independent review, full gates, scoped secret/diff audit.
6. Draft one submission-readiness PR for explicit approval. No external publication
   without approved text; no assistant merge. Deliver reproducible Codio handoff.
7. User runs native gates/phone checklist, merges when satisfied; verify final main.

## Slice A: task ownership and safe portrait publication

Likely files: app/conversion_tasks.py, bot.py, photo_flow.py, media.py; focused
new tests for early task ownership and download races. Keep registry name unless
there is an actual compatibility need; update its documentation for expanded use.

- Reserve/deduplicate work before the sequential handler returns. Worker uses the
  captured input/version; no raw Update or mutable state assumed safe after await.
- Reuse active/retired task tracking, error retrieval and shutdown hooks.
- Use the existing Telegram SDK memory-download API, never the shared disk path.
  Precheck advertised size and enforce the actual 10 MiB admission cap before
  decode/publication. SDK buffering precedes sink writes; document that this is
  not a transport-memory cap. No custom HTTP transport or dependency addition.
- Publish validated bytes only while current; retired finally blocks cannot delete
  replacement media. Preserve existing photo_dest contract for downstream stages.
- Tests coordinate through asyncio Events, not sleeps or polling. Include fakes
  returning after cancellation, expired deadlines and another chat progressing.

## Slice B: Bouncer and Interviewer workers

Likely files: photo_flow.py, orchestrator.py, bot.py, and touched logging wrappers.
New tests: reset/deadline matrix, interview delivery transactions and live handoff.

- Move external work off sequential update callbacks into the one registry.
- Bound download/model/send/notice operations; check explicit expiry and ownership.
- Preserve refusal text, six answers, summary contracts and accepted model IDs.
- Stage interview changes until successful guarded reply; retry cannot skip or
  double-record answers after a failed send. Retain old direct-helper contracts
  where compatible; test production orchestration separately. Do not preserve an
  unsafe runtime path just to pass historical tests. Resolve the five explicitly
  identified logging-contract conflicts using only the five approved exceptions.
- Start exactly one worker for the sixth-answer update. Summary, Converter,
  Scripter and Narrator retain that same asyncio task object and session version.
  Reuse the tracked convert_step/script direct nested-await pattern in bot.py,
  not standalone retry branches that call conversion_tasks.start. Assert task
  identity and exactly one registry.start for this turn in the live-wiring test.
- A cancellation-resistant old task remains tracked but cannot block a fresh
  session after reset. Shutdown retains bounded wait and honest pending reports.
- Replace unsafe exception logging in touched paths; keep cancellation propagation.

## Slice C: input fallback

Likely files: bot.py, handlers.py, telegram_adapter.py only if needed.
- Final fallback catches unsupported message types without shadowing existing routes.
- A new document MIME contract is unnecessary if all documents receive the same
  send-as-photo guidance. Do not expand supported image ingestion for this task.
- Apply stage-aware behaviour before existing text/photo/command retry paths, so
  unsupported media cannot start a Converter or Narrator retry accidentally.
- Verify through Application.process_update/registered handlers with transport
  mocked, including DONE and unknown commands, not just dispatch helper calls.

## Slice D: quality and documentation

- Prepare six synthetic answers spanning routine, social preference, stress
  response, hobby, habit and motivation. Use them for deterministic input-history
  tests; do not force those exact six topics into every live interview.
- Evaluate the rubric's depth requirement using a user-reviewed live sample only
  after code gates. Prompt refinement is conditional on observed thinness; do not
  count a canned fake summary as evidence of actual Gemini quality.
- Update README/TECH/ROADMAP and add diagrams from architecture.md with verified
  implementation details. Preserve MISSION and historical feature records.
- Final status must distinguish implemented, locally tested, Codio tested,
  user-phone accepted and merged. Do not mark this feature accepted in advance.

## Diff and review boundaries

Expected production touchpoints are listed above; no change planned to accepted
Converter/Scripter/Narrator provider models or audio_format/audio_encoding modules.
Shared orchestration changes must retain all later-stage ownership and codec gates.
No requirements.txt changes or mass formatting. The only approved baseline-test
exceptions are the five documented privacy exceptions and two pipeline fixture/
synchronization adaptations retaining every assertion (user approval 2026-10-18). If an unrelated defect demands
broader work, report it and seek approval rather than hide it in this PR.

Independent review focuses on: cancellation-resistant download safety, mutable
session references, send-before-commit, same-worker continuation, handler priority,
privacy and correspondence between diagrams and code. Reviewer verdict alone does
not replace executable tests. All commits use explicit paths; no git add .

## Delivery artifacts

- Scoped PR, approved title/body, requirements/plan/architecture/validation records.
- Exact verified Codio import/push commands if authenticated local push remains
  unavailable; leave uploaded bundles and patches untracked.
- Final native test output and user phone acceptance attached or referenced without
  credentials or private payloads. Public repo/main SHA verified after user merge.
