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
- Implementation gates: **439 passed**, including all 224 original cases and 215 added
  cases; ruff clean; mypy clean (18 source files). Unchanged `scripts/test` and
  `scripts/hooks` executed successfully on Windows/Python 3.13.12.
- All 19 original test/support files byte-identical to main; MISSION.md,
  requirements.txt and gate scripts unchanged.
- Installed Google ADK schema construction verified offline, with the runner mocked.
- Codio phone test: PENDING, must pass before merge.

## Test map
| Requirement | Automated evidence |
| --- | --- |
| Output schema, 90/91 words, format, lazy ADK, untrusted data | `tests/test_scripter.py` |
| Installed ADK accepts the output schema and text content | `tests/test_scripter_adk_compat.py` |
| Default storage and both reset commands | `tests/test_scripter_state.py` |
| Commit-after-send, missing traits, bad output, errors, timeouts, privacy | `tests/test_scripting_step.py` |
| Actual safe_send_message error privacy, direct and registry paths | `tests/test_scripter_privacy.py` |
| Same-task handoff, retry routing, resets, shutdown, real handler wiring | `tests/test_scripting_routing.py` |
| Final-answer handoff and two full mocked portrait-to-narration runs | `tests/test_scripter_handoff.py` |

## Red/Green evidence
- Session contract: 3 failures before field/reset implementation, then 3 passed.
- Agent contract: missing-module collection failure before `app/scripter.py` existed.
- Scripting step: 23 failures before implementation, then passing in integrated suite.
- Routing: 23 failures (unsupported scripter argument / missing LazyScripter) before
  bot wiring, then 23 passed against the real module, without the evidence-only stub.
  Final routing suite: 27 passed, including both resets during generation and sending
  through real registered handlers with a cancellation-resistant fake provider.
- Review correction: ordinary colons and prose parentheticals initially rejected;
  positive regression fixtures recorded failures before narrowing the rules.
- Full gate logs retained with the delivery artifacts. Windows runs use the existing
  Python environment and command shims to execute unchanged scripts/test and
  scripts/hooks. Outbound socket connections are blocked; only Windows asyncio's
  internal socketpair is allowed. No credentials or provider network are used.

## Manual Codio phone checklist (not yet performed)
- [ ] Run scripts/test and scripts/hooks on the feature branch in Codio.
- [ ] Avatar followed by exactly one plain-text paragraph, no more than 90 words.
- [ ] Tone is grand, affectionate, PG, grounded in supplied traits; no invented species.
- [ ] /restart during active scripting returns to photo prompt, no stale narration.
- [ ] /start during active scripting returns to photo prompt, no stale narration.
- [ ] Two complete runs without reboot, resetting between runs.
- [ ] No audio/voice message and no automatic Feature 6 work.
- [ ] Confirm .env remains untracked and ignored.
- [ ] User explicitly approves merge after reporting phone-test results.

No merge or auto-merge is authorized by automated success alone.
