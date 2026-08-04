# Dialogue Quality Correction V1

Status: R&D candidate for founder listening test.

## Problem

The prior prompt forced exactly one question, a 15-word ceiling, and appointment progression. Broad style retrieval injected booking examples into unrelated turns. Founder testing observed repeated slot offers, ignored topic changes, weak misunderstanding repair, and formal/robotic Tamil.

## Correction

- latest-request relevance takes priority over prior booking flow;
- questions are asked only when necessary;
- adaptive spoken length up to 35 words for direct explanation/repair;
- explicit topic-change and repair states disable slot offers;
- booking procedure is distinguished from booking request;
- general dental education is answered briefly without diagnosis;
- natural Chennai Tamil uses genuine English/Tanglish terms;
- intent-aware style examples are optional and limited to two;
- bounded post-LLM guards prevent irrelevant slot offers, false confirmation, and exact repetition.

## Controlled model replay

Frozen set: `voice-lab/voice_eval/frozen/dialogue-quality-v1.json`.

Both models used the same corrected system prompt and synthetic scenario inputs through the official Anthropic SDK.

| Arm | Relevant | Unwanted slot offers | Errors | Mean completion latency |
|---|---:|---:|---:|---:|
| Claude Haiku 4.5 corrected | 9/9 | 0 | 0 | 1,266 ms |
| Claude Opus 4.8 corrected | 9/9 | 0 | 0 | 2,286 ms |

Representative corrected Haiku outputs:

- Tooth types: `Front teeth incisors, அடுத்து canines, அதுக்கப்புறம் premolars, பின்னாடி molars ங்க.`
- Topic change: `சரிங்க, என்ன question சொல்லுங்க?`
- Repair: `Sorry, நான் தவறா புரிஞ்சுக்கிட்டேன். நீங்க கேட்டது மறுபடி சொல்லுங்க?`
- Procedure: `உங்க details கேட்டு read back பண்ணுவேன்; இது demo, actual booking save ஆகாது.`
- Tanglish fee: `Consultation fee ₹300 ங்க.`

## Experimental decision

Keep corrected Haiku 4.5 for the next founder test because it matched the frozen relevance gates while completing about 45% faster than Opus 4.8 in this small run. Opus remains the quality-ceiling challenger; it is not rejected or selected for production.

## Limitations

- Nine synthetic scenarios are insufficient for model selection.
- Automated relevance scoring is rule-based and must be supplemented by native founder listening.
- STT errors remain an independent major limitation.
- Live interruption, audio, and natural conversation must be retested with Cartesia unchanged.
- This does not establish pilot or production readiness.
