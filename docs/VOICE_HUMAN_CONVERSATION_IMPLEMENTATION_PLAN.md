# Fonely Human-Like Voice Lab — Implementation Plan

**Status:** Approved planning document; implementation is not started by this document.

## Context

Fonely already has a JavaScript voice feasibility prototype:

- `src/server.js` serves `public/index.html`.
- `src/browser-handler.js` accepts push-to-talk WebM clips, calls Saaras v3 REST STT, uses the Sarvam chat endpoint through `src/llm.js`, and streams Bulbul v3 audio.
- `src/sarvam.js` contains early REST/HTTP-streaming/WebSocket STT and TTS helpers.
- `src/call-handler.js` contains an early Exotel streaming path.

The prototype proves API connectivity, but it is not a natural real-time conversation. Microphone input is uploaded as a complete clip, STT currently shells out to `curl`, responses wait for final generated text, PCM network chunks are played as separate buffers, and there is no true barge-in or cancellation. Language selection is mostly script-based, secrets can reach command strings in the legacy path, and logs can contain raw customer/call content.

The goal is to let Karthick quickly experience a warm, human-like Tamil/Tanglish and Indian-English conversation while creating reusable voice assets: low perceived latency, natural spoken phrasing, controlled emotion, correct pronunciation, streaming, interruption handling, and per-turn quality/cost evidence.

This is a parallel experimental track. It is not production voice and does not replace Fonely's deterministic transaction backend.

## Recommended architecture

```text
Browser microphone (16 kHz mono PCM)
        ↓
Voice session + generation/cancellation IDs
        ↓
Sarvam Saaras v3 streaming STT
        ↓ finalized utterance + language evidence
Conversation adapter (current Sarvam chat for the experiment)
        ↓ structured speakable response plan
Speech renderer
  language/register + emotion + pronunciation + numbers/dates/prices
        ↓ semantic sentence chunks
Sarvam Bulbul v3 persistent WebSocket TTS
        ↓ sequenced PCM frames + timing metadata
Browser audio scheduler
        ↕
VAD/barge-in → stop local playback, cancel stale generation, listen immediately
```

Keep three boundaries separate:

1. **SpeechRecognizer** — audio in, normalized transcript events out.
2. **ConversationEngine** — transcript/history in, a short structured speakable plan out. It remains a demo adapter and performs no authoritative transactions.
3. **SpeechSynthesizer** — approved speakable chunks in, normalized audio events out.

Provider-specific messages remain inside the Sarvam adapters. Session and orchestration code must not depend on Sarvam wire formats. Every event carries `sessionId`, `turnId`, and `generationId` so stale STT, conversation, and TTS work can be discarded after interruption.

## Ownership and isolation

- Developer: Dev1, on a new branch such as `dev1/voice-human-conversation-lab` and a clean worktree such as `/scratch/karthick/fonely-worktrees/dev1-voice-human-conversation-lab`.
- Base: the latest clean integration baseline containing the existing JavaScript prototype. Inspect the actual HEAD before branching.
- Preserve `/scratch/karthick/fonely/CLAUDE.md`; never clean, stash, stage, overwrite, or commit it.
- Do not work in `/scratch/karthick/fonely-worktrees/dev1-phase-c-inventory-orders`.
- No migrations, database writes, appointment/order/inventory code, public tools, WhatsApp, deployment, commit, or push without separate authorization.
- Internal browser lab only initially. Bind localhost by default; require explicit opt-in for LAN/public binding. Use synthetic business data and consenting testers only.

# Phase-by-phase implementation

## Phase 0 — Freeze and measure the current demo

**Purpose:** Establish a reproducible baseline before changing architecture.

### Work

- Preserve the existing `/demo` and `/demo-ws` flow as a comparison path.
- Add safe configuration validation around existing environment names. Use `SARVAM_API_KEY` without printing it or exposing it to the browser.
- Remove shell-command STT construction from the new path; do not pass secrets through a shell.
- Define monotonic per-turn timestamps for microphone end, STT final, conversation start/end, TTS request, first audio, and playback start/end.
- Add a small synthetic script set for Tamil/Tanglish, Indian English, names, services, prices, dates, confirmation, confusion, and empathy.

### Reuse

- Endpoint/model knowledge from `src/sarvam.js`.
- `src/llm.js` demo business prompt only as baseline behavior.
- Targets and rubric from `docs/qa/VOICE_AND_LANGUAGE_EVAL.md`.

### Acceptance

- Existing demo still runs.
- Baseline report captures internal/browser timings for at least 20 scripted turns and does not claim PSTN evidence.
- New instrumentation logs no key, authorization header, phone number, or full transcript.

## Phase 1 — Voice studio: naturalness and emotion bake-off

**Purpose:** Deliver the fastest experiential milestone before real-time microphone work.

### Work

- Add a local `/voice-lab` page with typed text, language, speaker, pace, style/emotion preset, and sample-rate controls.
- Query the current Sarvam voice catalog/config where supported; otherwise use a versioned candidate list verified against the live API.
- Define controlled presets: `warm_greeting`, `helpful_question`, `empathetic_recovery`, `clear_confirmation`, `positive_success`, and `neutral_information`.
- Generate the same standardized utterance matrix across shortlisted voices and presets.
- Add A/B playback and 1–5 native-listener ratings for naturalness, comprehension, pronunciation, warmth, and preference.
- Store only non-sensitive local experiment metadata and generated sample IDs. Audio output remains ignored by Git.

### Critical files

- Modify `src/server.js` for explicit lab routes.
- Create `public/voice-lab.html`, `public/voice-lab.js`, and `public/voice-lab.css`.
- Create `src/voice/providers/sarvam-tts.js`, extracting/reusing behavior from `src/sarvam.js` without breaking old callers.
- Create `src/voice/eval-corpus.js` and `src/voice/telemetry.js`.

### Acceptance

- Karthick can compare at least five voices for Tamil/Tanglish and Indian English using identical phrases.
- First-audio and total synthesis latency are shown per sample.
- Ratings export as sanitized local JSON/CSV with provider model/version/config and no API key.
- Select one primary voice and one fallback per initial language through blinded listening.

## Phase 2 — Speakable response and pronunciation engine

**Purpose:** Make generated content sound like human speech instead of written chatbot prose.

### Work

- Replace free-form output on the new lab path with a strict internal `SpeakableResponsePlan` containing `speech`, `languageStyle`, `emotion`, `pace`, `chunks`, and `interruptible`.
- Support `ta-IN`, `tanglish`, and `en-IN` first; add `hi-IN`/`hinglish` later.
- Keep responses to one or two short spoken sentences and at most three choices.
- Implement deterministic normalization for currency, times, dates, phone digits, ranges, abbreviations, and punctuation pauses.
- Add a business pronunciation lexicon with owner-approved spoken forms for business names, staff, services, localities, and brands.
- Keep provider-specific pronunciation hints inside the TTS adapter.
- Test Tamil script, Tanglish, English, and mixed-script alternatives rather than forcing fully monolingual output.
- Derive emotion from bounded conversation state, never from unrestricted stage directions.

### Critical files

- Create `src/voice/speakable-plan.js`, `src/voice/normalize-speech.js`, `src/voice/pronunciation.js`, and `src/voice/chunk-speech.js`.
- Create a synthetic lexicon fixture under `src/voice/fixtures/`; store no customer data.
- Wrap or minimally modify `src/llm.js` while preserving the old demo.

### Acceptance

- Unit tests cover rupee amounts, 12/24-hour times, dates, ranges, phone digits, names, and code-switching.
- No semantic chunk splits a number, staff/service name, date/time, or confirmation fact.
- Native listener review finds no critical pronunciation defect in the seed corpus.

## Phase 3 — Continuous browser audio and streaming Saaras STT

**Purpose:** Remove whole-recording upload latency and create a live conversation loop.

### Work

- Capture browser microphone audio using Web Audio/AudioWorklet as 16 kHz mono PCM frames; retain push-to-talk as fallback.
- Send bounded frames over `/voice-lab-ws` using explicit start/audio/stop/flush events and payload limits.
- Build one persistent Saaras v3 STT WebSocket per voice session with readiness, timeout, close-code handling, reconnect policy, flush, and normalized events.
- Add bounded server-side session queues and one in-flight user turn.
- Reject oversized messages and stale generation IDs.
- Use final utterance events for conversation decisions; interim/VAD events may update UI but must not create duplicate turns.
- Remove temporary files and `execSync(curl ...)` from the new path.

### Critical files

- Create `public/voice-capture-worklet.js` and `public/voice-audio.js`.
- Create `src/voice/contracts.js`, `src/voice/providers/sarvam-stt.js`, `src/voice/session.js`, and `src/voice/browser-transport.js`.
- Modify `src/server.js` to register the new WebSocket route.

### Acceptance

- Continuous speech produces one finalized transcript per utterance without complete WebM upload.
- Tamil/Tanglish and Indian-English utterances work in at least 20 internal turns.
- Disconnect, empty transcript, timeout, malformed frame, and provider failure do not hang the session.
- Speech-end → STT-final latency is measured independently.

## Phase 4 — Persistent streaming Bulbul TTS and smooth playback

**Purpose:** Reduce silence and remove audible gaps/clicks between provider chunks.

### Work

- Use one persistent Bulbul WebSocket per session through a robust `SpeechSynthesizer` adapter with queued semantic chunks, first-audio timing, completion, timeout, and cancellation.
- Begin TTS when the first complete speakable sentence is available.
- Attach generation IDs to audio frames so the browser discards stale audio.
- Replace independent network-chunk playback with contiguous scheduling on one audio timeline and a small adaptive jitter buffer.
- Retain REST TTS as fallback and use a consistent output sample-rate/codec contract.
- Pre-generate only a small safe set of complete phrases after selecting a voice; do not stitch arbitrary personalized fragments.

### Critical files

- Complete `src/voice/providers/sarvam-tts.js`.
- Create `src/voice/tts-queue.js` and `src/voice/cache.js`.
- Extend `public/voice-audio.js` for contiguous scheduling and cancellation.

### Acceptance

- UI reports conversation-end → first-audible-audio latency.
- Standard corpus playback has no systematic gaps or clicks.
- REST fallback works after forced WebSocket failure.
- Cache keys include provider/model/voice/language/style/pace/format/sample rate; personalized audio never enters a global cache.

## Phase 5 — Barge-in and human turn-taking

**Purpose:** Make the experience conversational rather than walkie-talkie-like.

### Work

- Add conservative client-side VAD for interruption signaling and retain manual interrupt as a diagnostic control.
- On caller speech during playback: stop playback, clear queued audio, increment generation ID, cancel/ignore stale output, continue STT input, and preserve only actually played assistant words in history.
- Begin testing with headphones. Treat browser speakerphone echo cancellation as measured behavior, not a guarantee.
- Add bounded silence prompts and graceful recovery.

### Critical files

- Create `public/voice-vad.js` and `src/voice/turn-controller.js`.
- Extend session, transport, and audio scheduler contracts with cancel/interrupt events.

### Acceptance

- Barge-in stops audible playback within a measured 300 ms target on supported test hardware.
- Interrupting speech is not lost and stale audio never resumes.
- Noise/echo clips remain below the agreed false-interrupt threshold.
- Rapid interruption, disconnect during TTS, and provider timeout leave no orphan sockets/tasks.

## Phase 6 — Quality, cost, and provider-routing evidence

**Purpose:** Turn the demo into a measurable voice-moat foundation.

### Work

- Apply `docs/qa/VOICE_AND_LANGUAGE_EVAL.md`: semantic entity accuracy, code-switch preservation, pronunciation, native-listener MOS/preference, TTFA, p50/p95 latency, repeats, interruptions, transfers, and completion.
- Attribute provider/model/config usage and estimated cost per turn; calculate cost per successful scripted task.
- Add deterministic failure injection for STT/TTS/LLM timeout, disconnect, malformed events, and slow first audio.
- Define health-aware provider contracts, but add no second paid provider until Sarvam measurements identify a real deficiency.
- Prepare fixtures for future Google/Azure STT and commercially licensed Fish TTS without production credentials.

### Critical files

- Create `src/voice/metrics.js`, `src/voice/evaluator.js`, and `src/voice/providers/contracts.js`.
- Change the machine-readable `evals/` corpus only through a separate reviewed assignment.

### Acceptance

- A repeatable report compares voice/language/config on identical cases.
- Results record model version, language, voice, sample rate, codec, date, and pricing source.
- No provider is promoted from studio output alone.
- Experimental thresholds pass before real phone testing.

## Phase 7 — Real telephony validation (separate authorization)

**Purpose:** Verify browser quality through the actual 8 kHz/PSTN path.

### Work

- Reuse the provider-neutral session/turn controller in `src/call-handler.js`; do not maintain a second conversation engine.
- Confirm current Exotel media framing and codec/sample-rate requirements.
- Add media pacing, backpressure, stop/clear semantics, transfer/human fallback, and recording consent.
- Evaluate receiving-side recordings through TTS → codec → Exotel → phone.
- Use consenting internal/design-partner calls only; perform no real medical or booking transaction.

### Acceptance

- At least 20 consented internal calls with latency, pronunciation, barge-in, failure, and cost evidence.
- Browser and PSTN metrics remain separate.
- No pilot/production claim before documented Beta thresholds pass.

# Testing strategy

- Use Node's built-in `node:test` and `assert` for pure modules.
- Add focused `test:voice` scripts without a new framework unless necessary.
- Unit-test normalization, chunking, pronunciation selection, state transitions, generation cancellation, cache keys, and timing aggregation.
- Contract-test mocked Sarvam WebSocket events, reconnect/timeout/finalization, TTS completion, and cancellation.
- Manually verify browser microphone, smooth playback, interruption, reconnect, and mobile browser behavior.
- Provider smoke tests are opt-in and skip clearly when `SARVAM_API_KEY` is absent. They never print the key or raw headers.
- Keep generated audio, ratings, recordings, and temporary artifacts ignored from Git.

# Phase gates and reporting

Each phase is a separate bounded Dev1 assignment and stops for experiential review. Every report includes changed files, exact commands, tests, sample counts, latency evidence, limitations, secret/PII confirmation, and Git status.

Do not combine all phases in one patch. Do not commit or push without separate authorization.

The first authorization should cover **Phases 0–1 only**. This gives Karthick a voice-comparison experience quickly and selects the winning Sarvam voice/config before streaming and barge-in architecture is built around it.
