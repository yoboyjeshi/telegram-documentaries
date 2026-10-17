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

> **Current status:** Features 1–3 are implemented. The bot connects via long
> polling; `/start` and `/restart` greet, reset the chat, purge its temp media,
> and ask for a photo. Send a portrait photo and the **Bouncer** — a Google ADK
> `gemini-3.1-flash-lite` agent — confirms there's a human face, advancing the
> chat to the interview stage; a non-human photo gets a humorous refusal so you
> can try again. When the Bouncer accepts you, the **Interviewer** (also a
> `gemini-3.1-flash-lite` ADK agent) asks **6 questions** one at a time about
> your quirks, habits, and personality, remembers your answers per chat, then
> synthesises a typed trait summary and hands the chat to the converting stage
> (nothing happens after that yet). A photo sent mid-interview gets a gentle
> redirect; a blank message re-asks the current question; an unknown command
> nudges you to answer or `/restart`. The remaining stages above (Converter,
> Scripter, Narrator) land in later features.

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
   (Project dependencies are added as they land — see `SPECS/TECH.md` for the stack.)

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
