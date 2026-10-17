# Feature 5: The Scripter

Approved by BRIJESH DUTT on 2026-10-18. Baseline: 3dc2ad4.

## 1. Goal and scope
Automatically continue after the Converter successfully sends its avatar:

1. Converter sends the avatar and advances to SCRIPTING.
2. Generate narration from the stored TraitSummary.
3. Validate one plain-text paragraph.
4. Send the narration to Telegram.
5. Store the delivered narration and advance to NARRATING.
6. Stop. No TTS or audio.

One successful attempt produces **one narration message**. No alternate scripts, editing commands, new stages, database, new dependencies or Narrator implementation.

The existing roadmap and state machine already define this step and the SCRIPTING → NARRATING transition. [1, 2]

## 2. Requirements
### 2.1 Agent and input
Add **app/scripter.py** containing:
- SCRIPTER_MODEL = "gemini-3.1-flash-lite".
- A typed Scripter interface.
- AdkScripter, with an injectable runner for offline tests.
- LazyScripter, which obtains credentials and creates its delegate only on first use.
- A validated Pydantic output model, proposed name NarrationScript, containing text.

**Proposed interface:**

`async def write_script(summary: TraitSummary) -> NarrationScript`

Use Google ADK LlmAgent and a fresh ADK session per generation, following the existing Interviewer pattern. No tools or external actions are exposed to the agent. [3]

**Input is the stored TraitSummary only.** No image rereading or new avatar-analysis call. Because the current Converter does not store the generated animal's identity, the Scripter must not claim to know the avatar's exact species or appearance.

### 2.2 Prompt and tone
The fixed agent instruction requires:
- An authoritative British nature-documentary narrator persona.
- Grand, affectionate, lightly tongue-in-cheek narration.
- PG language; no mockery of the user or their appearance.
- Grounding in supplied traits, without inventing personal facts.
- Exactly one paragraph, at most 90 words.

Serialize the summary and traits as a clearly labelled **untrusted data payload**, separate from the fixed instruction. Explicitly prohibit following commands contained in that payload.

This is prompt-injection defence, not a claim that prompting can guarantee perfect model behaviour. Structural validation remains mandatory.

### 2.3 Output validation
The paragraph must pass validation **before any narration is sent or saved**.

| Rule | Proposed definition |
| --- | --- |
| Type | Strict string inside the expected schema; reject wrong types or malformed JSON |
| Length | 1–90 whitespace-separated words; contractions and hyphenated words count as one |
| Paragraph | No embedded line breaks, paragraph separators or control characters |
| Format | No Markdown/HTML formatting, code fences, lists, headings or stage-direction wrappers |
| Emoji | Reject emoji and emoji-sequence markers |
| Telegram limit | Fit within one Telegram message, with no splitting |

Trim surrounding whitespace only. **Do not truncate, rewrite or flatten invalid output to make it pass.** Invalid output takes the retry path.

Use the same validation for real ADK output and injected results. Formatting rules will have explicit positive and negative fixtures, including ordinary apostrophes and punctuation.

### 2.4 Session storage and successful delivery
Add:

`SessionState.narration_text: str | None = None`

**Commit only after successful delivery:**
1. Generate and validate the candidate locally.
2. Confirm session ownership and SCRIPTING.
3. Send through the existing guarded message helper.
4. Recheck ownership after the await.
5. Store the exact delivered text and advance to NARRATING, without another await between these mutations.

This resolves the tension between “store and send” and **“Telegram failure leaves state untouched.”** A failed send must not leave a committed narration behind.

Both reset commands clear narration_text.

### 2.5 Failure and retry behaviour
| Situation | Behaviour |
| --- | --- |
| Missing interview or trait summary | Ask for /restart; no model call; remain at SCRIPTING |
| Empty, malformed or overlength output | Friendly retry; no narration sent or stored |
| Gemini failure or timeout | Friendly retry; state unchanged |
| Telegram send fails or returns False | Friendly retry; state unchanged |
| Retry notification also fails | Safely log metadata; remain retryable; no notification loop |
| Cancellation or stale session | Exit silently; no retry, narration commit or advancement |

**Proposed user messages:**
- Retry: “My documentary writer is having a moment. Send a text message to try again, or /restart to start over.”
- Missing summary: “I seem to have lost your interview notes. Send /restart and we'll begin again.”

A later text, photo or non-reset command at SCRIPTING retries scripting only, **not conversion**. No automatic model retry loop.

Proposed deadlines: **120 seconds for generation, 30 seconds for sending**, matching the current hardening pattern.

## 3. Task ownership and reset design
**Reuse the existing task registry and keep one active task per chat.** Do not add a second scripting registry or concurrent Telegram update handlers.

The current registry rejects a second task while one is active. Therefore, the conversion worker will **continue directly into scripting inside the same task**, carrying the same session version. It must not call start() again for that handoff. [4]

For retries from SCRIPTING, start a registry task that runs only the scripting step.

Preserve these invariants:
- Duplicate updates never start overlapping generation.
- Different chats remain independent.
- /start and /restart invalidate ownership before cancellation, purge media and clear session data.
- Check stage, version and cancellation before work, after generation, before sending and after sending.
- An SDK returning after cancellation cannot commit or initiate a stale send.
- Preserve CancelledError; do not catch it as an ordinary failure.
- Bot shutdown cancels scripting through the existing lifecycle hook.
- Once at NARRATING, do not rerun scripting or automatically start another stage.

**External boundary:** a message already accepted by Telegram cannot be recalled. A lost delivery acknowledgement can also make a retry duplicate an externally delivered message. We will not claim exactly-once delivery across network failures.

### 3.1 Logging
New scripting paths log only operation, chat ID, stage and exception type. Never log trait text, narration, keys, raw exceptions or validation payloads.

The existing generic @guard_errors logs tracebacks, so it must **not** wrap the new step unchanged. Follow the Converter's explicit safe exception handling and reuse safe_send_message. Broader earlier-stage logging cleanup remains outside Feature 5. [5]


MISSION.md remains unchanged. No Narrator/TTS work.
