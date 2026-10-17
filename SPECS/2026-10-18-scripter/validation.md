# Validation record

## 5. Required tests and acceptance gates
All Gemini and Telegram calls mocked; no network or real credentials.

- Successful generation receives the correct traits, sends one paragraph, stores that exact paragraph and advances once.
- Accept 90 words; reject 91, empty/whitespace, wrong types, malformed schema, line breaks, formatting, emojis and stage directions.
- Missing summary avoids both model and narration-send calls.
- Gemini/send failures and timeouts preserve the original session state.
- Retry succeeds without regenerating the avatar.
- Logs contain no sentinel keys, traits, narration or exception payloads.
- Both resets during generation, during send and at the Converter handoff.
- Cancellation-resistant fake model returns late without stale sends or state writes.
- New-session narration/media cannot be overwritten or deleted by old tasks.
- Duplicate updates, cross-chat independence and shutdown.
- Real registered Telegram handlers exercised with mocked transport, not direct dispatcher tests alone.
- Full photo → narration ordering, followed by **no TTS/audio call and no automatic work at NARRATING**.

**Gates:** preserve all 224 existing regression cases and their intent, add Scripter coverage, pass scripts/test and scripts/hooks. No tests disabled to accommodate the new stage. New full-flow tests will explicitly inject the Scripter.

**Phone acceptance:** avatar then narration, correct tone/format, both mid-scripting resets, and two complete runs without reboot.

## 6. Constitution reconciliation
- Keep MISSION.md unchanged.
- Preserve current reset behaviour: greeting/photo prompt, ending at AWAITING_PHOTO.
- Update ROADMAP's stale Feature 4 “merge pending” status.
- Do not include Feature 6 or broader Feature 7 cleanup.


## Execution evidence
- Baseline: 224 passed (offline), clean checkout at 3dc2ad4.
- Implementation gates: pending.
- Codio phone test: PENDING, must pass before merge.
