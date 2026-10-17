# Target architecture: submission readiness

These diagrams describe the readiness design, now implemented on the feature
branch and under final review. Reconciled diagrams also appear in SPECS/TECH.md,
linked from README. Local execution evidence and outstanding corrections are in
validation.md; native Codio and phone acceptance are pending. No ADK parent
orchestrator is claimed.

```mermaid
flowchart TD
    TG[Telegram chat] -->|Long polling| Router[Sequential update handlers]
    Router -->|start or restart| Reset[Invalidate, cancel, clear state and purge chat media]
    Router -->|Schedule or deduplicate| Tasks[One per-chat task registry: ConversionTasks]
    Router -->|Unsupported input| Guidance[Stage-aware guidance, DONE silent]
    Tasks --> Flow[Application orchestration and ownership guards]
    Flow <--> State[In-memory per-chat session and interview history]
    Flow --> Download[SDK memory download then size admission check]
    Download -->|Current ownership only| Media[Chat-namespaced portrait publication]
    Flow --> Bouncer[Bouncer ADK role: flash-lite]
    Flow --> Interview[Interviewer ADK role: flash-lite]
    Flow --> Converter[Converter ADK role: flash-image]
    Flow --> Scripter[Scripter ADK role: flash-lite]
    Flow --> Narrator[Direct Gemini TTS integration: flash-tts-preview]
    Media -->|Original portrait bytes| Bouncer
    Media -->|Original portrait bytes| Converter
    State -->|Six-answer traits in same request| Converter
    State -->|Stored trait summary| Scripter
    State -->|Stored delivered narration| Narrator
    Narrator --> Codec[Validated PCM or WAV to FFmpeg OGG Opus]
    Flow --> Send[Bounded ownership-guarded Telegram sends]
    Codec --> Send
    Send --> TG
    Reset --> State
    Reset --> Media
    Reset --> Tasks
```

All four named agent roles use ADK LlmAgent/InMemoryRunner. Role invocations may
have fresh ADK sessions; durable conversation continuity within this bot process
comes from application-owned history supplied to each turn. Nothing persists
across a process restart. Exact model IDs stay those of the accepted baseline.

```mermaid
stateDiagram-v2
    [*] --> GREETING
    state "Active session" as Active {
        AWAITING_PHOTO --> BOUNCER_CHECK: normal Telegram photo
        BOUNCER_CHECK --> AWAITING_PHOTO: reject or intake failure
        BOUNCER_CHECK --> INTERVIEW: human accepted
        INTERVIEW --> INTERVIEW: one question and answer at a time
        INTERVIEW --> CONVERTING: sixth answer and summary confirmed
        CONVERTING --> SCRIPTING: avatar delivered
        SCRIPTING --> NARRATING: paragraph delivered and stored
        NARRATING --> DONE: voice delivery confirmed
        CONVERTING --> CONVERTING: owned failed attempt then manual retry
        SCRIPTING --> SCRIPTING: owned failed attempt then manual retry
        NARRATING --> NARRATING: owned failed attempt then manual retry
    }
    GREETING --> AWAITING_PHOTO: start or restart
    Active --> Reset: start or restart from any substate
    Reset --> AWAITING_PHOTO: invalidate, cancel, clear, purge and prompt
```

The composite Active box groups existing stages for the diagram, and Reset is a
reset operation; neither is a new Stage enum member. Interview failure does not record an answer or advance. DONE has no
non-reset outgoing action. Late/stale work is discarded, not a state transition.

## Ownership and delivery rules

- One active worker per chat; retired workers remain observable until completion.
- Intake worker continues Bouncer -> first question. Every accepted answer starts
  exactly one owned turn, including the sixth. The sixth-answer task continues
  summary -> Converter -> Scripter -> Narrator by direct nested awaits, with no
  second registry.start or replacement asyncio task.
- Before external send or local publication: verify version, expected stage,
  cancellation state and deadline. Check again after await before committing.
- No shared disk path is supplied to a still-running photo download. This prevents
  old downloads from recreating media after reset.
- Already accepted Telegram messages cannot be recalled. Lost acknowledgements
  can cause duplicate external delivery after a manual retry; no exactly-once claim.
- Reset must not wait for a model call. A malicious/non-cooperative dependency can
  outlive cancellation; keep results unusable and report unfinished shutdown work.
