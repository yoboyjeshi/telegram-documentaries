# Feature 6: The Narrator

Status: APPROVED by BRIJESH DUTT on 2026-10-18: 'Approve the spec and implement'.
Baseline: merged main 0dec5956c42201538d8c6adb1fa3ef5762d96fe7 (Feature 5, PR #5).
Branch: feature/2026-10-18-narrator.

## 1. Scope and success

Complete the existing pipeline automatically after the Scripter stores narration_text
and advances to NARRATING. Render that exact paragraph, validate the audio, save a
chat-namespaced artifact, deliver a playable Telegram voice message, then advance
to DONE and stop. Do not resend the avatar or regenerate the script.

Narrator is a Gemini integration, NOT an ADK agent. Use exactly
`gemini-3.1-flash-tts-preview`, with no fallback model or automatic model retry loop.
No database, web UI, background music, video, voice cloning or Feature 7 cleanup.
MISSION.md remains byte-for-byte unchanged.

## 2. Integration and input contract

Proposed app/narrator.py:
- Narrator protocol: async render(narration_text: str) -> NarrationAudio.
- GeminiNarrator: direct asynchronous google-genai generate_content call, not ADK.
- LazyNarrator: obtain the existing Settings key on first use; no network/key
  requirement at import or application construction.
- Inject a typed provider seam for fully offline tests. Use explicit API-key
  configuration, never credentials in prompts, logs, subprocess arguments or repr.
- Pydantic NarrationAudio boundary: strict bytes plus declared audio MIME;
  exclude audio data from repr. Revalidate injected/model_construct results too.
- One single-speaker AUDIO response per attempt. Reject absent/ambiguous audio
  output rather than assuming candidates[0].content.parts[0] always exists.
- Proposed fixed voice: Charon. Request authoritative British documentary delivery,
  warm and measured. Treat the stored paragraph as read-aloud data, never commands;
  request it verbatim, without spoken instructions or invented text. No new LLM
  rewrite or transcription verification call. Voice quality is phone-tested.
- Missing, empty or whitespace-only narration_text: no provider call, friendly
  /restart message, unchanged NARRATING. Do not reconstruct text from traits.

Use the installed google-genai SDK already brought in by google-adk; declare it
as a direct compatible dependency if production imports it directly. Validate the
actual installed SDK request schema in offline compatibility tests before pinning.
Disable SDK automatic request retries for this integration: one TTS attempt per
accepted user-triggered retry or automatic Scripter handoff.

## 3. Audio format decision and validation

Gemini 3.1 TTS returns headerless PCM by default. The current Google documentation
explicitly distinguishes this from newer models' default WAV. Do NOT copy a newer
model example, change the requested model, or rename raw PCM to .ogg/.mp3. [1]
Telegram documents playable voice messages as OGG/Opus, MP3 or M4A; sendAudio's
music-player contract is MP3/M4A. WAV is not the chosen playback contract. [2, 3]

Proposed delivery path: validated PCM -> correctly wrapped WAV -> FFmpeg OGG/Opus
-> guarded safe_send_voice. This adds an explicit system dependency: FFmpeg with
libopus support. No automatic installation or privileged system changes. Check
availability and document Codio setup; request approval if installation is needed.
Missing encoder or unsupported codec follows the safe retry path, never a silent
WAV/document fallback. No need to add both send_voice and send_audio implementations.

Accept only the provider formats actually needed:
- audio/L16 (case-insensitive), codec=pcm when supplied, rate=24000, mono,
  signed 16-bit little-endian as specified by this Gemini endpoint. Validate MIME
  parameters; reject contradictory rate/codec/channel declarations. Missing channel
  metadata uses the documented mono endpoint contract, not arbitrary guessing.
- PCM WAV declared as audio/wav or audio/x-wav: RIFF/WAVE structure and complete
  decodable frame data, 24 kHz, mono, 16-bit uncompressed PCM. Never double-wrap WAV.
- Reject all other MIME/codec combinations, missing MIME, empty/non-byte payloads,
  truncated WAV, odd-length PCM, implausible duration or excessive size. Unsupported
  formats fail closed; this feature is not a general-purpose audio converter.

Proposed limits: max 10 MiB at each byte boundary, duration greater than zero and
at most 120 seconds, checked before encoding and again before sending. Check full
frame lengths/container consistency, not just magic bytes. Raw PCM has no container
signature: alignment/metadata validation cannot prove that samples contain speech.
Do not claim content-level or exact spoken-word verification from byte validation.

Encode at mono 24 kHz, Opus 48 kbps, OGG container. FFmpeg receives only bounded audio
via pipes, with no shell, network inputs, narration or keys in arguments. Limit
output accumulation; require successful exit, valid OGG/Opus metadata and a successful
bounded decode of the final artifact before sending. Reject malformed/truncated
encoded output and excessive decoded duration, not just missing OggS/OpusHead magic.
Keep stderr out of application logs. Tests cover failures at both validation boundaries.

The WAV intermediary stays in memory. Persist the verified final bytes as
`<media_root>/<chat_id>/narration-<session_version>-<attempt_id>.ogg`.
The extension therefore matches actual delivered encoding. Never persist a raw PCM
blob under a WAV extension. Server-generated attempt IDs, no user-controlled paths.

## 4. Orchestration and guarded delivery

Add handle_narrating_update in app/orchestrator.py and safe_send_voice plus its typed
injected capability in app/bot.py, mirroring safe_send_photo's boolean result and
metadata-only logging. No generic @guard_errors/traceback logging on new paths.

One successful attempt:
1. Check owning session version, NARRATING and task cancellation state.
2. Read stored narration_text; handle missing text without a TTS call.
3. Generate, validate and encode. Recheck ownership after every external await.
4. Revalidate final artifact, recheck ownership, create the chat directory and write
   the attempt-owned .ogg file. No await between final ownership check and local write.
5. Recheck ownership and send the voice file through the guarded helper.
6. After the send await, recheck ownership, NARRATING, task cancellation and deadline.
7. Only if delivery was confirmed, advance NARRATING -> DONE, with no intervening await.
8. Clean up the generated artifact and stop. No additional model work or completion send.

Deadlines: 120 seconds for generation plus normalization/encoding/validation;
30 seconds for voice sending, and 30 seconds for a single failure/missing-input
notification. Encoder work also has a 15-second sub-limit within the 120-second
budget. Preserve explicit deadline.expired() checks for cancellation-resistant
providers/senders; a late return is not success. asyncio cannot forcibly stop a
provider that suppresses cancellation forever, but its late result must be unusable.

On timeout/reset/shutdown, terminate and reap encoder subprocesses (kill after a
short bounded grace if needed); use cancellation-safe cleanup and re-raise
CancelledError. Encoding uses pipes rather than writing files in the background,
so a retired process cannot recreate reset media directories.

## 5. Failure behaviour and privacy

| Situation | Required result |
| --- | --- |
| Missing narration | Ask for /restart, no TTS call, remain NARRATING |
| Bad MIME/empty/malformed/oversized audio | No audio send; friendly retry; unchanged state |
| TTS, encoding, validation or filesystem error/timeout | Friendly retry; unchanged state |
| Telegram returns False, raises or times out | No DONE; friendly retry; unchanged state |
| Failure notification also fails | Safely log metadata; no notification loop |
| Cancellation or stale ownership | Exit silently; no stale notification/send/state mutation |
| Cleanup fails after confirmed delivery | Safely log metadata; retain DONE, do not resend |

Proposed messages:
- Retry: "My narrator is having a moment. Send a text message to try again, or /restart to start over."
- Missing input: "I seem to have lost your narration. Send /restart and we'll begin again."

Only operation/chat ID/stage/exception type and non-sensitive counters may be logged.
Never raw exception text, traceback, keys, narration, SDK response, audio bytes or
encoder stderr. Test sentinel secrets/narration in exceptions at every new boundary.
Capture broader SDK/transport loggers too to detect leakage from new integration use.

Clean the attempt's generated audio on success, failure and cancellation, with
ownership guards. Retired work must never overwrite/delete replacement-session
files. Reset continues to purge the entire chat directory, including portraits,
images and audio; both commands clear state/narration and return to AWAITING_PHOTO.
Do not broaden this change into earlier-stage portrait-lifetime or logging cleanup.

## 6. Task registry, routing and terminal behaviour

Reuse ConversionTasks, its monotonic session versions and existing post_stop hook.
Do not rename/rebuild the registry or enable concurrent Telegram update handlers.
- Scripter -> Narrator continues directly in the SAME registered task and version.
  Do not call registry.start() a second time during the automatic handoff.
- A retry at SCRIPTING may continue into Narrator after scripting succeeds.
- Text/photo/non-reset command at NARRATING starts only narration, never another
  script, conversion or photo download. Existing /start and /restart priority remains.
- Duplicate active updates are deduplicated; chats remain independent.
- DONE returns without starting any pipeline task, model call or download. Reset
  commands still win. No automatic restart or narration replay at DONE.
- Keep direct dispatch's optional dependency seam for isolated earlier-stage tests;
  the real application always supplies LazyNarrator and the shared registry.

Check ownership before work/writes/sends and after awaits, including failure handling.
A cancellation-resistant model returning after reset or shutdown must not initiate
an audio send, recreate files, mutate state or emit a stale failure notification.

External-delivery limit: audio already accepted by Telegram cannot be recalled by
reset. A lost acknowledgement can cause duplicate delivery on a later manual retry.
Guarantee one transition for a successful owned attempt and no overlapping TTS tasks,
not distributed exactly-once Telegram delivery across network failures.

## 7. Approval and delivery boundaries

User approved this spec and implementation on 2026-10-18. Delivery: Red/Green implementation,
all 439 baseline tests plus new tests, ruff/mypy, independent review, one Feature 6
PR, then PAUSE for Codio phone testing. The assistant must not merge.

Update README/TECH/ROADMAP during implementation to mark Feature 5 phone-tested and
merged at 0dec595, and Feature 6 in progress/pending phone test. Replace obsolete
live-behaviour claims that NARRATING stops, preserving historical Feature 5 evidence.
Check .env untracked/ignored and excluded from each proposed commit and commit tree.
Never copy Codio credentials locally. Leave MISSION.md unchanged.

## References

[1] https://ai.google.dev/gemini-api/docs/speech-generation (including migration notes
    explicitly distinguishing Gemini 3.1 raw PCM from newer default WAV output).
[2] https://core.telegram.org/bots/api#sendvoice
[3] https://core.telegram.org/bots/api#sendaudio

Local baseline reviewed: SPECS/MISSION.md, TECH.md, ROADMAP.md, Feature 5 requirements
and plan; app/bot.py, orchestrator.py, converter.py, conversion_tasks.py, media.py,
states.py, session_store.py, handlers.py, config.py; README, dependencies and routing
fixtures. Baseline source and architecture, not new live Gemini/Telegram calls.
