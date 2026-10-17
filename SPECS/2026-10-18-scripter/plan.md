# Approved implementation plan

Branch: `feature/2026-10-18-scripter`, clean checkout from updated main.

## 4. Implementation plan after approval
Each implementation step follows failing tests first, then the smallest change to make them pass.

| Step | Work |
| --- | --- |
| 1. Establish branch | Verify updated remote main; create feature/2026-10-18-scripter from it in a clean checkout. Preserve the old local Converter work untouched. |
| 2. Record approved spec | Add SPECS/2026-10-18-scripter/requirements.md, plan.md and validation.md. |
| 3. Contracts | Add narration validation, session field and reset clearing. |
| 4. Agent | Implement lazy ADK construction, fixed instructions, data-only input and injected runner. |
| 5. Stage step | Add handle_scripting_update in app/orchestrator.py, guarded send, safe failure handling and commit-after-send semantics. |
| 6. Wiring | Add automatic Converter handoff and scripting-only retries within the existing registry. Preserve direct test entry points and sequential handlers. |
| 7. Verification and docs | Run full gates, independent review, update README/TECH/ROADMAP, then pause for Codio phone testing before merge. |

No general workflow framework or broad registry rewrite. Existing ConversionTasks compatibility stays intact; its documentation and diagnostic labels can describe both stages.


User authorized one Feature 5 PR; PAUSE for Codio phone testing before merge. Never merge automatically. Verify .env is untracked before every commit.
