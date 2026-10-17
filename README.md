# The Telegram Documentaries

A Telegram bot that turns a selfie into an Attenborough-style wildlife mini-documentary.

## What it does
Upload a portrait photo in Telegram and the bot runs a five-stage pipeline:

1. **Bouncer** — confirms the photo is a human (rejects non-humans with a humorous refusal).
2. **Interviewer** — asks 5 to 7 questions about your quirks, habits, and personality.
3. **Converter** — generates a hybrid-animal avatar from your photo plus those traits.
4. **Scripter** — writes one dramatic nature-documentary narration paragraph.
5. **Narrator** — renders that paragraph to audio and sends the finished mini-documentary back to the chat as an image plus a voiceover.

Everything happens inside Telegram. There is no web frontend.

> **Current status:** Features 1–5 are merged and phone tested; Feature 6 is in progress (phone test pending). The bot connects via long
> polling; `/start` and `/restart` greet, reset the chat, purge its temp media,
> and ask for a photo. Send a portrait photo and the **Bouncer** — a Google ADK
> `gemini-3.1-flash-lite` agent — confirms there's a human face, advancing the
> chat to the interview stage; a non-human photo gets a humorous refusal so you
> can try again. When the Bouncer accepts you, the **Interviewer** (also a
> `gemini-3.1-flash-lite` ADK agent) asks **6 questions** one at a time about
> your quirks, habits, and personality, remembers your answers per chat, then
> synthesises a typed trait summary and hands the chat to the converting stage.
> The **Converter** (`gemini-3.1-flash-image`, a Google ADK agent) then
> reimagines your original portrait as a hybrid-animal avatar matching those
> traits and sends it back as a Telegram photo with a one-line documentary
> caption, advancing the chat to `SCRIPTING`. The **Scripter** then uses
> `gemini-3.1-flash-lite` through Google ADK to write and send one affectionate
> nature-documentary paragraph, at most 90 words. The delivered text is stored
> for the **Narrator**, a direct `gemini-3.1-flash-tts-preview` integration (not ADK).
> It validates audio, produces an OGG/Opus voice message and advances to `DONE`
> only after Telegram confirms delivery.
> The generated avatar is written to the chat's temp dir (`avatar.png` or `avatar.jpg`, matching decoded format) and
> deleted once sent; `/restart` purges it along with the portrait. If
> generation or the photo send fails, the bot asks you to try again and leaves
> the chat at `CONVERTING` so your next message retries. A photo sent
> mid-interview gets a gentle redirect; a blank message re-asks the current
> question; an unknown command nudges you to answer or `/restart`. The
> Narrator is the final pipeline step; `DONE` starts no further work.

## Quickstart

1. **Clone the repo**
   ```bash
   git clone https://github.com/yoboyjeshi/telegram-documentaries.git
   cd telegram-documentaries
   ```

2. **Create your environment file and fill in the keys**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and set:
   - `TELEGRAM_BOT_TOKEN` — talk to [@BotFather](https://t.me/BotFather) in Telegram to create a bot and get its token.
   - `GEMINI_API_KEY` — from Google AI Studio.

   `.env` is git-ignored. Never commit real values.

3. **Install dependencies**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   Feature 6 also requires **FFmpeg with libopus and OGG support on PATH**, for
   Telegram-playable voice output. Check before running (no installation):
   ```bash
   ffmpeg -version
   ffmpeg -hide_banner -encoders
   ffmpeg -hide_banner -muxers
   ffmpeg -hide_banner -demuxers
   ffmpeg -hide_banner -decoders
   ```
   Confirm `libopus` is an encoder, `ogg` is both a muxer and demuxer, and an
   Opus decoder and `s16le` output muxer are available for final decode validation.
   A build with libopus but no OGG muxer is insufficient.
   If support is missing, arrange an approved installation for your Codio environment;
   the bot does not install it. Missing codec support leaves narration retryable,
   never sends a disguised WAV file. Run the full test gate to verify actual encoding
   and decoding, not just the advertised capabilities.

4. **Run the bot (long polling)**
   ```bash
   python -m app.main
   ```
   The bot uses Telegram **long polling**, so it reaches outbound over HTTPS — no public IP, SSL certificate, or ngrok required.

## Development

Run these from the repo root:

- **Tests** — `./scripts/test` runs the `pytest` suite (`python3 -m pytest`).
  Extra arguments are forwarded, e.g. `./scripts/test -k states`.
- **Lint & types** — `./scripts/hooks` runs `ruff check app tests` followed by
  `mypy app`.

Both scripts are the project's gates; a change is only ready when both are green.
There is no network access and no real Telegram involved in the tests: Telegram
sends are mocked and secrets stay out of the suite.

## Project constitution
The project contract lives in `SPECS/`:

- `SPECS/MISSION.md` — what we're building, tone, the non-negotiable principles, and the definition of done.
- `SPECS/TECH.md` — stack and architecture (long polling, Google ADK state machine, Gemini 3.1 models, resilience rules).
- `SPECS/ROADMAP.md` — build order, one SDD loop per feature.

## Secrets
Never hard-code, commit, or log secrets. `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` are read from `.env` only; `.env.example` documents the required keys.

## Converter hardening (Feature 4)
Conversion, scripting and narration share one tracked per-chat background task. Telegram update
handlers remain sequential. Both `/start` and `/restart` invalidate the old
session before cancellation and purge its media; late results cannot initiate
a photo send, advance the new session, or delete its files. A send already
accepted by Telegram cannot be recalled.

PNG/JPEG output is decoded and validated with Pillow (rerun dependency install).
Generation has a 120-second deadline; photo sending has a 30-second deadline.
Failures stay at `CONVERTING` for a text/photo/command retry. Cancellation is not
a failure notification. Converter/send errors log metadata, never raw exceptions.
Generated images are cleaned on success, failure and cancellation. A filesystem
refusing deletion is logged without rolling back an already-delivered image.

Earlier-stage interruptibility and wider resilience remain Feature 7 work.
Feature 4 phone tests passed and PR #4 was merged at `3dc2ad4`.

## Scripter (Feature 5)
The Scripter reads only the stored `TraitSummary`, never the photo or avatar.
Traits are labelled untrusted data, not instructions. Its fixed prompt requests
British documentary narration: grand, affectionate, PG, and never mocking.
The exact avatar species is not available, so narration must not invent it.

`NarrationScript` validates a strict plain-text paragraph (1–90 whitespace-separated
words), without Markdown/HTML, emoji, stage directions or internal line breaks.
Invalid output is rejected, never truncated. Generation is capped at 120 seconds
and each send at 30 seconds. Failed generation, validation or delivery leaves
`SCRIPTING` unchanged and offers a text-message retry. Photos and non-reset
commands also retry only scripting, without regenerating the avatar. Missing
traits prompt `/restart`. The narration is saved only after successful delivery.

Both reset commands cancel scripting, clear stored narration and purge media.
Late results cannot initiate stale sends or overwrite the new session. An already
accepted Telegram message cannot be recalled, and a lost send acknowledgement
can cause a duplicate on retry; delivery is not exactly-once.

Feature 5 passed Codio phone testing and PR #5 was merged at `0dec595`.

## Narrator (Feature 6, in progress)

The Narrator reads only stored `narration_text`, using the direct asynchronous
Gemini SDK and `gemini-3.1-flash-tts-preview`, fixed Charon voice. No ADK agent,
model fallback, script rewrite or automatic TTS retry loop. The Scripter hands off
inside the same registered task; text/photo/non-reset commands at `NARRATING`
retry only audio. Missing narration asks for `/restart` without calling Gemini.

Validate declared PCM/WAV, wrap raw PCM as WAV in memory, encode using FFmpeg to
OGG/Opus and validate the final audio before upload. Limits: 10 MiB per artifact,
positive duration up to 120 seconds. Raw PCM metadata/alignment checks cannot prove
that samples contain intelligible speech; voice quality is checked on the phone.
Generation/preparation deadline is 120 seconds, codec work 15 seconds within that
budget, and delivery/notifications 30 seconds each.

The audio path is chat/session/attempt-specific with a truthful `.ogg` extension.
Advance `NARRATING -> DONE` only after confirmed delivery and ownership/deadline
checks. Failed generation, validation, filesystem work or delivery leaves the
session unchanged and offers a later retry. Metadata-only logs never include
narration, keys, raw exceptions, audio or encoder stderr.

Both reset commands cancel the shared task, purge all chat media and clear stored
narration. Shutdown also cancels narration. Late results cannot start stale sends,
recreate old media or mutate replacement sessions. Generated audio is removed on
completion/failure/cancellation. A Telegram send already accepted cannot be recalled;
lost acknowledgements can duplicate an externally delivered voice on manual retry.

Offline tests mock all Gemini and Telegram traffic; codec tests use local FFmpeg
without network and must not be skipped in the final gates. See
`SPECS/2026-10-18-narrator/plan.md` for acceptance tests and the Codio phone checklist.
One Feature 6 PR, phone test before merge, no automated merge.
