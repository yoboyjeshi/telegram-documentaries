# MISSION — The Telegram Documentaries

## What we are building
A Telegram bot that turns a user's selfie into a dramatic, David-Attenborough-style wildlife mini-documentary. Pipeline: confirm the photo is a human, interview the user about quirks and habits, generate a hybrid-animal avatar from the photo plus traits, write a dramatic nature-documentary narration, and deliver it back to the chat as an image and an audio voiceover.

## End-to-end user experience
1. User starts a session in Telegram and uploads a portrait photo.
2. Bot confirms a human face (rejects non-humans with a humorous refusal).
3. Bot asks a short interview (5 to 7 questions) about quirks, daily habits, personality.
4. Bot generates a hybrid-animal avatar from the photo plus traits.
5. Bot composes a single dramatic nature-documentary narration paragraph.
6. Bot renders the narration to audio and sends the finished mini-documentary (image plus voiceover) back to the chat.
All interaction happens inside Telegram. There is NO web frontend.

## Tone
Narration persona is an authoritative, dramatic British nature-documentary narrator: grand, affectionate, a little tongue-in-cheek. Bouncer refusals are humorous and light, never rude. Keep it fun, warm, PG. No mockery of the user's appearance.

## Core principles (non-negotiable)
1. State always resets cleanly. /start and /restart must fully reset the session at ANY stage: cancel in-progress work, purge all temporary media (uploaded photo, generated image, generated audio), return to greeting. The bot must never require a process reboot to recover. This is tested by TAs multiple times in one running process.
2. Never crash. Every external call (Telegram, Gemini) is wrapped so a failure notifies the user and offers a retry; the process stays alive.
3. One session per chat. Each chat has independent session state.
4. Secrets stay in .env. TELEGRAM_BOT_TOKEN and GEMINI_API_KEY are read from .env only. Never hard-coded, committed, or logged.
5. Honest scope (YAGNI). Build only what the pipeline needs.

## Definition of done
A user can run the full pipeline end-to-end on a phone, twice in a row, in the same running process, without a restart, and /restart at any stage cleanly returns them to the start.
