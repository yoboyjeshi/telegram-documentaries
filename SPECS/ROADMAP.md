# ROADMAP — Build order (one SDD loop per feature)

Build in order. Each feature is one SDD loop: spec, Build orchestrator, review, manual phone test, merge PR. Start a fresh OpenCode session per feature to conserve tokens.

1. Telegram Gateway: connect via long polling using TELEGRAM_BOT_TOKEN from .env; implement /start and /restart (greet plus reset); echo text replies to confirm the pipe on the phone; establish per-chat session store and state-machine scaffold. Done when the bot replies on the phone and /restart resets to GREETING. **Status: implemented** (2026-10-11, branch `feature/2026-10-11-telegram-gateway`; code and tests complete pending manual phone test and PR merge).
2. The Bouncer: on photo upload, download to a chat-namespaced temp path; Bouncer agent confirms a human face; human advances to INTERVIEW, non-human gets a humorous refusal and stays in AWAITING_PHOTO. Done when a selfie advances and an object/pet photo is rejected without crashing.
3. The Interviewer (+ orchestrator): 5 to 7 pointed questions one at a time; maintain state; collect answers into a trait summary; this agent orchestrates remaining stages. Done when the bot interviews and produces a trait summary.
4. The Converter: pass original portrait plus interview summary to flash-image; generate hybrid-animal avatar; send image to chat. Done when a generated animal-hybrid image arrives after the interview.
5. The Scripter: flash-lite British documentary-narrator persona; ONE dramatic paragraph, max about 90 words, plain text only. Done when a single narration paragraph is produced from the traits.
6. The Narrator: send the Scripter paragraph through flash-tts-preview; save audio to a chat-namespaced temp path; send as a Telegram voice/audio message; mark DONE. Done when a voiceover plays back in the chat.
7. Resilience and reset hardening: /start and /restart fully reset from EVERY stage; try/except on every Telegram and Gemini call with notify plus retry; handle edge cases (photo during interview, text during Bouncer, API timeout); temp-media hygiene. Done when a TA-style stress test passes with no reboot.
8. End-to-end verification and submit: run the full pipeline twice in a row on the phone, same process, no restart; confirm .env never committed and repo is public; merge final PR; submit the public repo.

Keep each PR focused on one feature. If logic drifts, update the specs rather than patch snippets. The Narrator is an integration, not an ADK agent.
