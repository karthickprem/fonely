# Voice and Language Evaluation Framework

This document defines how Fonely evaluates voice quality, language accuracy,
and conversational latency across supported languages, providers, and
deployment conditions.

---

## Native-Speaker Rating Rubric

All subjective evaluations use a 1-5 scale rated by native speakers of the
target language.

| Score | Naturalness             | Comprehension                | Cultural Appropriateness        |
|-------|-------------------------|------------------------------|---------------------------------|
| 5     | Indistinguishable from  | Understood everything on     | Fully appropriate greetings,    |
|       | a human speaker         | first listen                 | honorifics, and phrasing        |
| 4     | Slight synthetic        | Understood with minimal      | Minor awkwardness but           |
|       | quality, still natural  | effort                       | acceptable                      |
| 3     | Noticeably synthetic    | Understood most, replayed    | Some phrasing feels foreign     |
|       | but intelligible        | a few parts                  | but not offensive               |
| 2     | Robotic, requires       | Missed significant parts     | Inappropriate register or       |
|       | effort to follow        |                              | phrasing                        |
| 1     | Unintelligible          | Could not understand         | Offensive or nonsensical        |

Each evaluation session requires a minimum of 3 native-speaker raters per
language. Scores are averaged; inter-rater agreement is tracked.

---

## STT Word and Intent Accuracy

### Word Error Rate (WER)

```
WER = (Substitutions + Insertions + Deletions) / Total Reference Words
```

- Measured per language and dialect using a labeled audio corpus.
- Audio corpus must include telephone-quality recordings (8 kHz, mu-law) as
  well as clean recordings.
- WER is reported separately for:
  - Clean audio
  - Telephone audio (8 kHz)
  - Noisy environments (background conversation, street noise)

### Intent Error Rate (IER)

```
IER = Number of incorrect intents / Total utterances
```

- Measured after the full pipeline: audio -> STT transcript -> intent
  classification.
- Captures compounding errors (STT transcription mistakes that cause
  downstream intent misclassification).
- Broken down by language, intent category, and provider.

---

## TTS Pronunciation and Naturalness

### Mean Opinion Score (MOS)

- Rated by native speakers on the 1-5 naturalness scale defined above.
- Tested on a standardized set of utterances per language that includes:
  - Common conversational phrases.
  - Business-specific terms (service names, product names, addresses).
  - Numbers, dates, times, and currency amounts.
  - Proper nouns (business names, personal names).

### Pronunciation accuracy

- Evaluated by native speakers listening for mispronounced words.
- Domain-specific terms are weighted more heavily (a mispronounced business
  name is a critical defect).
- Measured as percentage of utterances with zero pronunciation errors.

---

## Romanized and Code-Switching Evaluation

Many Fonely users communicate in romanized Indian languages or switch between
languages mid-sentence.

### Tanglish (Tamil + English)

- STT must handle Tamil words written/spoken in English phonetics.
- TTS must pronounce Tamil words naturally even when the input is romanized.
- Eval corpus includes Tanglish utterances with known reference transcripts.

### Hinglish (Hindi + English)

- Same requirements as Tanglish, applied to Hindi + English mixing.

### Mixed-script inputs

- The LLM must handle inputs that mix Devanagari, Tamil script, and Latin
  script within a single conversation.
- Eval corpus includes multi-script examples with expected intent labels.

### Measurement

- WER and IER are reported separately for code-switched utterances.
- A dedicated slice of the eval corpus is tagged `code_switch` for filtered
  reporting.

---

## Telephone 8 kHz Quality

All voice evaluations must be conducted at telephone bandwidth to reflect
real-world conditions.

- Audio is downsampled to 8 kHz, mono, mu-law encoded before evaluation.
- STT WER measured at 8 kHz must meet the same thresholds as the pass criteria
  (see below).
- TTS MOS is measured after the audio passes through a simulated telephone
  codec path (G.711).
- Studio-quality evaluations are recorded for reference but are not used for
  pass/fail decisions.

---

## Time-to-First-Audio

- **Target**: < 500 ms from the end of the caller's speech to the first byte
  of TTS audio arriving at the telephony gateway.
- Measured as:

  ```
  TTFA = timestamp(first TTS audio byte sent) - timestamp(end of caller speech detected)
  ```

- Includes STT finalization, LLM inference (time to first token), and TTS
  synthesis startup.
- Reported as P50 and P95 across a representative sample of turns.

---

## P50 / P95 Turn Latency

Total pipeline latency for a single conversational turn:

```
Turn latency = STT duration + LLM inference duration + TTS synthesis duration
```

- **P50 target**: < 1.5 seconds.
- **P95 target**: < 3.0 seconds.
- Measured end-to-end in the production (or staging) environment, not in
  isolated benchmarks.
- Broken down by provider combination (e.g., Sarvam STT + DeepSeek LLM +
  Fish Audio TTS) to identify bottlenecks.

---

## Barge-In

- When the caller speaks while TTS audio is playing, the system must:
  1. Detect the interruption (voice activity detection on the incoming stream).
  2. Stop TTS playback immediately.
  3. Begin STT processing on the caller's new utterance.
- Evaluation criteria:
  - Detection latency: time from caller speech onset to TTS stop (target
    < 300 ms).
  - No dropped words: the STT transcript of the interrupting utterance must
    be complete.
  - No echo: the TTS audio that was playing must not contaminate the STT
    input.

---

## Silence and Noise Handling

### Silence

- If the caller is silent for a configurable period (default 5 seconds), the
  system prompts with a follow-up (e.g., "Are you still there?").
- After a second silence timeout, the system ends the call gracefully.
- The silence threshold is configurable per business.

### Background noise

- The system must tolerate typical telephone background noise (traffic, other
  conversations, TV) without hallucinating words.
- STT providers are evaluated on a noisy-audio slice of the eval corpus.
- False-positive rate (system "hears" speech when there is only noise) must be
  below 5%.

### Unclear audio

- When STT confidence is below a threshold, the system asks the caller to
  repeat rather than guessing.
- The confidence threshold is tunable per language.

---

## Provider Comparison Matrix

Each provider combination is evaluated across all dimensions defined in this
document. Results are recorded in a comparison matrix.

| Dimension               | Sarvam STT | Sarvam TTS | Fish Audio TTS | DeepSeek | Qwen   | Llama  |
|-------------------------|------------|------------|----------------|----------|--------|--------|
| WER (Tamil, 8 kHz)      |            |            | --             |          |        |        |
| WER (Hindi, 8 kHz)      |            |            | --             |          |        |        |
| IER (Tamil)             |            |            | --             |          |        |        |
| IER (Hindi)             |            |            | --             |          |        |        |
| MOS (Tamil)             | --         |            |                | --       | --     | --     |
| MOS (Hindi)             | --         |            |                | --       | --     | --     |
| TTFA P50 (ms)           |            |            |                |          |        |        |
| Turn latency P95 (ms)   |            |            |                |          |        |        |
| Cost per turn (INR)     |            |            |                |          |        |        |

- Cells marked `--` are not applicable (e.g., WER for a TTS-only provider).
- The matrix is regenerated after every provider upgrade or configuration
  change.

---

## Cost Per Turn and Per Call

Every conversational turn incurs costs from multiple providers:

```
Cost per turn = STT cost + LLM cost (input + output tokens) + TTS cost
Cost per call = SUM(cost per turn for all turns) + telephony cost (per-minute)
```

- Costs are tracked per provider and per language.
- The eval framework logs estimated cost alongside quality metrics so that
  cost-quality tradeoffs are visible.
- Telephony cost (Exotel per-minute rate) is included in the per-call total.

---

## Pass Thresholds

Three maturity levels define when a language + provider combination is ready
for progressively wider deployment.

### Experimental

- IER < 40%
- No MOS requirement.
- Suitable for internal testing and demo calls only.

### Beta

- IER < 20%
- MOS > 3.0 (TTS naturalness rated by native speakers).
- Suitable for pilot deployments with consenting businesses.

### Verified

- IER < 10%
- MOS > 3.5
- P95 turn latency < 3.0 seconds.
- Barge-in detection latency < 300 ms.
- Suitable for general availability.

### Promotion process

- A language + provider combination starts at Experimental.
- Promotion to Beta requires passing the Beta thresholds on at least 200 eval
  corpus utterances and 20 real calls.
- Promotion to Verified requires passing the Verified thresholds on at least
  500 eval corpus utterances and 100 real calls, plus a pilot acceptance review.
