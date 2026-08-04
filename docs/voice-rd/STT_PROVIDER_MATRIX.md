# STT Provider Matrix

Status date: 2026-08-04. This is a research-access matrix, not a production selection.

Evidence levels are separate:

1. documentation-proven;
2. direct account/API access;
3. measured native 16 kHz;
4. measured deterministic simulated 8 kHz;
5. native-listener review;
6. critical-field safety;
7. pricing estimate/invoice reconciliation;
8. production selection.

| Provider arm | Documentation-proven | Direct access | 16 kHz | Simulated 8 kHz | Native listener | Critical fields | Price evidence | Status |
|---|---|---|---|---|---|---|---|---|
| Sarvam Saaras v3 `transcribe` | 22 Indian languages + English; automatic detection; native-language output | Verified with existing account | 50 founder fixtures; 49 successful | Missing | Review incomplete | Semantic exactness 76%; failed gate | Historical run omitted pricing | Frozen failed V0 |
| Sarvam Saaras v3 `codemix` | Mixed English/Indic-script output | Verified with existing account | 50 founder fixtures; 50 successful | Missing | Live founder result: poor | Semantic exactness 82.4%; failed gate | Historical run omitted pricing | Frozen failed V0 |
| OpenAI `gpt-live-transcribe` | Recommended realtime model; delta/final events; language hints | Blocked: `OPENAI_API_KEY` unavailable | Not run | Not run | Not run | Not run | Official estimate $0.017/audio minute | Highest-priority access-blocked challenger |
| Google STT V2 `chirp_2` `ta-IN` | Tamil row documented in `asia-southeast1`; adaptation, confidence, punctuation, profanity filtering | Blocked: project/credentials unavailable | Not run | Not run | Not run | Not run | Not verified | Required challenger; live Tamil method still unproven |
| Azure stable Tamil-capable path | `ta-IN` listed; general realtime API documented | Blocked: key/region unavailable | Not run | Not run | Not run | Not run | Not verified | Required challenger; stable live Tamil intersection unproven |
| Speechmatics | Realtime service/pricing documented | Blocked: key unavailable; Tamil claim not promoted | Not run | Not run | Not run | Not run | Official pricing page exists | Exploratory only |
| AI4Bharat Tamil IndicConformer/IndicWav2Vec | Open artifacts useful for offline research | Repository/model access only; runtime/license audit incomplete | Not run | Not run | Not run | Not run | Self-hosted cost unmeasured | Offline/shadow exploratory |
| Deepgram Flux/Nova-3 | Current flagship language matrix reviewed | No adapter work authorized | Not run | Not run | Not run | Not run | Not relevant | Excluded until official Tamil evidence |
| Cartesia `ink-2` | Stable model; current official model page lists only `en` | No adapter work authorized; early Tamil access not granted | Not run | Not run | Not run | Not run | 3 credits/audio second; silence counts | Excluded from Tamil/Tanglish shortlist unless Cartesia grants early Tamil access |

## Primary sources

- [Sarvam STT overview](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/overview), accessed 2026-08-04.
- [Sarvam STT REST](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/rest-api), accessed 2026-08-04.
- [Sarvam Saaras models](https://docs.sarvam.ai/api/getting-started/models/saaras), accessed 2026-08-04.
- [OpenAI Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription), accessed 2026-08-04.
- [OpenAI `gpt-live-transcribe`](https://developers.openai.com/api/docs/models/gpt-live-transcribe), accessed 2026-08-04.
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing), accessed 2026-08-04.
- [Google Chirp 2](https://docs.cloud.google.com/speech-to-text/v2/docs/chirp_2-model), accessed 2026-08-04.
- [Google supported languages](https://docs.cloud.google.com/speech-to-text/v2/docs/speech-to-text-supported-languages), accessed 2026-08-04.
- [Google streaming recognition](https://docs.cloud.google.com/speech-to-text/v2/docs/streaming-recognize), accessed 2026-08-04.
- [Azure language support](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=stt), accessed 2026-08-04.
- [Azure realtime STT quickstart](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/get-started-speech-to-text), accessed 2026-08-04.
- [Deepgram model languages](https://developers.deepgram.com/docs/models-languages-overview), accessed 2026-08-04.
- [Speechmatics supported languages](https://docs.speechmatics.com/introduction/supported-languages), accessed 2026-08-04.
- [AI4Bharat IndicConformer](https://github.com/AI4Bharat/IndicConformerASR), accessed 2026-08-04.
- [Cartesia Ink 2 model page](https://docs.cartesia.ai/build-with-cartesia/stt/latest.md), accessed 2026-08-04.
- [Cartesia pricing](https://docs.cartesia.ai/pricing.md), accessed 2026-08-04.

## Research corrections

The adversarial primary-source pass refuted two claims, so they are not used:

- Sarvam base STT at ₹30/hour was not verified by the cited current source.
- Google `asia-southeast1` Chirp 2 being specifically “Private GA” was not verified.

No provider has been selected for production.
