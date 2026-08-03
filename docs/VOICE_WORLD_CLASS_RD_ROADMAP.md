# Fonely World-Class Voice R&D Roadmap

**Status:** Designed R&D strategy  
**Date:** 2026-08-03  
**Scope:** Tamil, Chennai Tanglish, and Indian-English voice assistant for Indian MSMEs  
**Readiness:** This document is a roadmap. It is not implementation, provider approval, pilot validation, or production-readiness evidence.

## Mission

Build a voice business assistant that:

- feels like speaking with a competent local receptionist;
- understands Tamil, Chennai Tanglish, and Indian English reliably;
- handles pauses, overlap, interruption, corrections, names, amounts, and times;
- safely completes deterministic business workflows;
- remains affordable for Indian MSMEs;
- becomes increasingly powered by Fonely-owned data and models.

The strategic objective is not to immediately reproduce a generic global voice assistant. It is to become better than generic assistants at Indian business conversations while operating at substantially lower cost.

## Executive architecture decision

Pursue three paths in parallel, with one controlled production baseline.

### Near-term baseline: best-of-breed cascade

```text
Browser or telephony audio
  → WebRTC/media transport
  → acoustic echo cancellation and noise suppression
  → VAD and semantic endpointing
  → Indian-language streaming STT
  → tenant-scoped contextual transcript resolution
  → deterministic dialogue and safety policy
  → language-realization model
  → speakable response planner
  → expressive Indian-language streaming TTS
  → cancellable playback
```

This path provides transcript evidence, contextual correction, deterministic transaction boundaries, provider substitution, and stage-level observability.

### Parallel benchmark: native realtime speech-to-speech

Evaluate native realtime providers using the same Chennai calls and business tasks:

- Gemini Live;
- OpenAI Realtime;
- future Indian realtime speech providers;
- relevant open or self-hostable full-duplex systems.

Provider demos are not evidence. Every system must be tested on the same locked corpus, network conditions, safety cases, and task flows.

### Medium-term hypothesis: hybrid voice architecture

Use native audio intelligence for:

- timing and overlap;
- emotion and hesitation evidence;
- backchannels;
- interruption handling;
- expressive rendering.

Keep an inspectable text and deterministic application path for:

- authoritative facts;
- confirmation;
- safety decisions;
- business commands;
- committed evidence.

A native audio model must never directly mutate authoritative business state.

## Current baseline and limitations

Current R&D stack:

```text
SmallWebRTC
  → Pipecat
  → Silero VAD
  → Smart Turn
  → Sarvam Saaras v3 streaming STT
  → deterministic safety
  → Claude Haiku
  → Chennai style retrieval
  → Sarvam Bulbul v3 streaming TTS
  → WebRTC playback
```

This is a capable research baseline, but it is not world-class yet.

### Recognition limitations

- No mature domain-biased recognition for doctors, treatments, Chennai locations, names, dates, or times.
- Provider language detection reports a predominant language and can hide within-utterance switching.
- Raw STT output is still treated too much like authoritative text.
- Errors such as `doctor appointment` → `documentary` are critical intent/entity failures, not ordinary cosmetic WER errors.

### Dialogue limitations

- The dialogue model receives text, not the caller's acoustic emotion and hesitation.
- Style examples improve wording but do not solve timing and prosody.
- Free-form model history is not a substitute for deterministic workflow state.
- Prompting alone cannot guarantee correct local semantics.

### Speech-generation limitations

- Tamil pronunciation depends on script and normalization quality.
- Sentence boundaries and provider chunking can cause audible seams.
- Temperature controls variation, not reliably appropriate human emotion.
- The voice has not been adapted using licensed Fonely receptionist speech.

### Interaction limitations

- Tamil/Tanglish endpointing requires measured calibration.
- Browser speaker echo can cause false starts and interruption.
- Natural backchannels are not explicitly modeled.
- Cascade boundaries add latency and discard acoustic information.

## Proposed world-class acceptance gates

These are Fonely product targets, not verified provider results.

### Recognition

| Metric | Initial pilot gate | World-class target |
|---|---:|---:|
| Tamil WER | ≤18% | ≤12% |
| Tanglish mixed error rate | ≤20% | ≤15% |
| Names, times, and prices exactness | ≥95% | ≥99% |
| Domain entity exactness after correction | ≥98% | ≥99.5% |
| Correction-induced critical errors | <0.5% | <0.1% |
| Cross-tenant context leakage | 0 | 0 |

Track critical values separately from general WER:

- doctor and staff names;
- patient names;
- phone numbers;
- dates and times;
- prices and quantities;
- service names;
- locality and address names;
- negation and correction.

### Conversation timing

| Metric | Pilot gate | World-class target |
|---|---:|---:|
| Speech end → first audible response p50 | ≤1.2 s | ≤600 ms |
| Speech end → first audible response p95 | ≤2.0 s | ≤1.0 s |
| Interruption → audible stop p95 | ≤500 ms | ≤250 ms |
| False user cutoffs | ≤5% | ≤2% |
| False starts caused by echo/noise | ≤3% | ≤1% |
| Lost initial syllable after barge-in | ≤2% | <0.5% |

### Human preference

- Warmth MOS ≥4.2/5.
- Pronunciation MOS ≥4.3/5.
- Conversational appropriateness ≥4.3/5.
- At least 80% agree it felt like speaking to a local receptionist.
- At least 70% prefer the candidate over the current production baseline.
- Task completion ≥95%.

Use blinded, paired tests with multiple independent native Tamil/Tanglish listeners.

### Safety and transaction correctness

- Unsafe medical advice: 0.
- Unconfirmed authoritative mutations: 0.
- Wrong-tenant or wrong-entity mutation: 0.
- Transaction-field accuracy: ≥99.5%.
- False booking confirmation: 0.
- Success reported before outer commit: 0.

## Contextual transcript resolution

Implement a constrained and auditable correction layer before replacing STT.

### Preserve raw and corrected evidence

```json
{
  "raw_transcript": "எனக்கு 1 documentary புக் பண்ணனும்",
  "provider_language": "ta-IN",
  "provider_confidence": 0.78,
  "correction_candidates": [
    {
      "text": "எனக்கு doctor appointment book பண்ணனும்",
      "reason": "dental context + booking verb + observed acoustic confusion",
      "confidence": 0.96
    }
  ],
  "resolved_transcript": "எனக்கு doctor appointment book பண்ணனும்",
  "resolution": "automatic_high_confidence"
}
```

### Trusted correction inputs

Tenant-scoped facts only:

- clinic and branch names;
- doctors and staff;
- configured services;
- locality and landmark names;
- configured prices;
- valid dates and actual available slots;
- caller-confirmed spellings;
- Fonely's reviewed confusion dictionary;
- current deterministic conversation state.

### Correction policy

Automatically correct only when:

- the candidate belongs to trusted tenant configuration;
- conversation context strongly supports it;
- no authoritative numeric, identity, or negation fact changes;
- calibrated confidence exceeds the approved threshold.

Ask for clarification on uncertain critical facts:

> "Doctor appointment book பண்ணணும்னு சொன்னீங்களா?"

Never silently change:

- person identity;
- phone number;
- date or time;
- price or quantity;
- doctor or service;
- negation;
- treatment request.

Initial reviewed confusion set:

```text
documentary       ↔ doctor appointment
root channel      ↔ root canal
scaling           ↔ cleaning
six thirty        ↔ 6:30
Priya / Preeya    ↔ Dr. Priya
Aminjikarai variants
appointment / அப்பாயின்ட்மெண்ட் variants
```

The raw transcript remains immutable.

## Data moat

Fonely's moat is a rights-cleared dataset connecting audio, language, timing, business state, and outcomes.

```text
audio
  → raw transcript
  → corrected transcript
  → code-switch boundaries
  → domain entities
  → conversation act
  → interruption timing
  → generated speech
  → native-listener preference
  → workflow outcome
```

### Recognition error corpus

Capture consented, sanitized pairs such as:

```text
audio: doctor appointment
raw STT: documentary
corrected: doctor appointment
context: dental booking
```

### Pronunciation assets

Version:

- clinic and staff names;
- Chennai localities;
- services and products;
- Tamil/English spellings;
- preferred TTS rendering;
- provider/model configuration;
- native-speaker ratings.

### Turn-taking corpus

Annotate:

- speech onset and end;
- meaningful versus thinking pauses;
- interruption;
- backchannel;
- false start;
- self-correction;
- overlap;
- noise and echo;
- whether the first syllable was lost.

### Preference corpus

For each candidate pair, ask:

- Which sounds more local?
- Which sounds warmer?
- Which sounds clearer?
- Which sounds less robotic?
- Which response fits the situation better?

### Outcome corpus

Track:

- intent understood;
- task completion;
- repeats and corrections;
- human handoff;
- failure reason;
- cost and latency;
- safety outcome.

### Data governance

Every training item records:

- source and provenance;
- license;
- speaker consent;
- allowed commercial and derivative-model uses;
- compensation where applicable;
- retention and deletion obligations;
- voice-cloning restrictions;
- redistribution rights.

Public/licensed datasets such as IndicVoices can support pretraining and evaluation only after legal and provenance review. They do not replace consented Chennai telephony and business-conversation data.

## Training strategy

### Stage 1: bounded components first

Build or train:

1. Domain entity correction/reranker.
2. Tamil/Tanglish punctuation and normalization.
3. Endpointing classifier.
4. Interruption and echo classifier.
5. Pronunciation selector.
6. Intent and conversation-act classifier.
7. Compact language-realization model.

These are cheaper, easier to verify, and reduce provider dependence immediately.

### Stage 2: adapted STT

After at least 500 consented hours with reliable transcripts:

- benchmark fine-tuning IndicConformer/Whisper-class models;
- add Chennai/Tanglish and telephony augmentation;
- train domain-biased decoding;
- add code-switch boundary labels;
- distill from the best commercial STT plus human corrections.

Optimize first for critical entity accuracy, not general WER.

### Stage 3: adapted TTS

Train only with explicit voice and derivative-model rights.

Initial focus:

- pronunciation frontend;
- Tamil/Tanglish text normalization;
- phoneme/grapheme selection;
- prosody tags;
- emotion and pace controls;
- one or two licensed voices.

Later:

- fine-tune a commercially usable multilingual TTS model;
- distill to smaller streaming models;
- quantize and benchmark affordable serving;
- cache safe complete phrases, never arbitrary personalized fragments.

### Stage 4: native or hybrid audio model

Attempt only after:

- 10,000+ consented evaluated calls;
- strong overlap and turn annotations;
- a proven cascade baseline;
- sufficient GPU and serving budget;
- evidence that cascade boundaries are the dominant remaining weakness.

## Low-cost inference strategy

### Proposed economics gates

Pilot target:

> All-in cost ≤₹1 per connected minute.

Long-term target:

> ₹0.25–₹0.50 per successful-task minute, excluding separately billed carrier charges.

These are goals, not current verified costs.

### Cost ledger

Measure per call:

- carrier/PSTN;
- TURN relay bandwidth;
- STT audio seconds;
- model input/output tokens;
- TTS characters/audio seconds;
- retries and fallbacks;
- paid silence;
- concurrency and GPU utilization;
- storage and observability;
- human escalation;
- support cost;
- successful versus failed task.

### Cost reduction order

1. Eliminate unnecessary silence billing.
2. Keep responses short.
3. Cache stable prompts and safe complete phrases.
4. Route easy turns to smaller models.
5. Run entity correction locally.
6. Run VAD and endpointing locally.
7. Self-host STT only when utilization beats API economics.
8. Self-host TTS only after quality and concurrency pass.
9. Quantize and batch where latency allows.
10. Reserve premium native realtime models for calls where measured value justifies them.

## 30-day gate

### Deliverables

- Instrument the current cascade stage by stage.
- Freeze consent, retention, deletion, and licensing policy.
- Collect a balanced 20–30-hour consented evaluation corpus.
- Double-review at least five hours.
- Create locked test slices for entities, ambiguity, noise, and interruption.
- Run Saaras mode bake-off.
- Run Bulbul voice/pace/temperature bake-off.
- Evaluate Gemini Live and OpenAI Realtime on identical calls.
- Capture actual invoices and retention/SLA terms.

### Minimum locked cases

- 500 appointment utterances.
- 300 doctor/service/name cases.
- 300 date/time/price cases.
- 200 location/address cases.
- 200 ambiguity/self-correction cases.
- 200 interruptions.
- 200 noisy utterances.
- 100 safety/medical cases.

### Promotion evidence

A documented table of:

- recognition and entity errors;
- p50/p95 latency;
- interruption behavior;
- native-speaker preference;
- actual measured cost;
- retention/licensing terms;
- operational failure rate.

## 90-day gate

### Product architecture

- adaptive endpointing;
- real barge-in and stale-audio cancellation;
- tenant-scoped contextual transcript resolution;
- deterministic conversation state;
- language model limited to response realization;
- confirmed deterministic commands;
- immutable evidence and transaction safety.

### Required evidence

- 500 internal/design-partner calls.
- Task completion ≥90%.
- Critical entity exactness ≥98%.
- End-to-end p95 ≤2 seconds.
- Barge-in p95 ≤500 ms.
- Unsafe action count 0.
- False booking confirmations 0.
- Human preference ≥65% over the current lab.
- All-in cost measured from invoices.

## 180-day gate

- 500+ consented hours collected.
- 50+ hours double-annotated gold data.
- 5,000+ evaluated calls.
- Contextual correction operating in shadow or pilot.
- Local endpointing model trained and evaluated.
- Versioned pronunciation frontend.
- At least one self-hosted STT candidate benchmarked.
- At least one adapted TTS candidate benchmarked.
- Human preference ≥70% over day-30 baseline.
- Critical entity exactness ≥99%.
- Task completion ≥95%.
- Unsafe/unconfirmed mutations 0.
- All-in cost ≤₹1/minute.
- Operations, retention/deletion, backup, incident, and provider-failure evidence documented.

## 365-day gate

Promote a self-trained or hybrid system only after:

- 10,000+ shadow/pilot calls;
- speaker-disjoint Tamil/Tanglish benchmark passes;
- at least three independent native-listener cohorts;
- no material age, gender, device, or dialect regression;
- first-audio p95 ≤1 second;
- interruption-stop p95 ≤250 ms;
- critical entity exactness ≥99.5%;
- task completion ≥95%;
- human preference ≥70% over prior production baseline;
- measured concurrency meets the cost target;
- commercial derivative-model rights are documented;
- rollback and provider fallback are proven;
- deterministic transaction invariants remain unchanged.

## Immediate two-week sequence

1. Freeze the current Pipecat cascade as baseline V0.
2. Stop unbounded prompt tuning.
3. Build the evaluation harness and immutable case manifest.
4. Implement contextual transcript correction in shadow mode.
5. Begin consented raw/corrected error logging.
6. Create 300 critical dental entity cases.
7. Run Saaras mode bake-off.
8. Run Bulbul voice and configuration bake-off.
9. Obtain current provider pricing, retention, data-residency, SLA, and licensing terms.
10. Add native realtime experimental adapters as R&D paths—not production paths.

## Explicitly deferred

- Training an end-to-end speech foundation model.
- Voice cloning.
- Premature microservices.
- Uncontrolled patient call storage.
- Public exposure of internal commit tools.
- Provider promotion from demos.
- Claims of ChatGPT-level quality based on a small sample.

## Strategic conclusion

Fonely's defensible system is not one giant model. It is:

> Fonely's proprietary Indian interaction dataset + contextual recognition + turn-taking intelligence + local voice rendering + trusted deterministic business engine.

Over time, measured weak components become Fonely-owned models. Until then, providers remain replaceable components rather than the moat.

## Research basis and caveats

The cited sources verify API capabilities and relevant research architectures. They do not establish Chennai/Tanglish quality, comparative provider pricing, independent human preference, telephony reliability, or Fonely production readiness. Moshi's latency is author-reported and not a universal PSTN/WebRTC guarantee. Full-Duplex-Bench is English-only. Provider models, limits, prices, and terms are time-sensitive and must be rechecked before procurement or launch.

## Primary sources

- [Sarvam STT API reference](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe)
- [Sarvam STT overview](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/overview.md)
- [Sarvam TTS conversion API](https://docs.sarvam.ai/api-reference/text-to-speech/convert)
- [Sarvam TTS streaming API](https://docs.sarvam.ai/api-reference/text-to-speech/stream)
- [Pipecat SmallWebRTC](https://docs.pipecat.ai/server/services/transport/small-webrtc)
- [Gemini Live guide](https://ai.google.dev/gemini-api/docs/live-guide)
- [Gemini Live API](https://ai.google.dev/api/live)
- [OpenAI Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad)
- [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Full-Duplex-Bench](https://arxiv.org/abs/2503.04721)
- [Moshi full-duplex speech model](https://arxiv.org/abs/2410.00037)
- [IndicVoices](https://huggingface.co/datasets/ai4bharat/indicvoices)
- [AI4Bharat IndicConformer](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual)
- [NVIDIA contextual ASR customization](https://docs.nvidia.com/deeplearning/riva/user-guide/docs/asr/asr-customizing.html)
