# Founder Realtime POC Test Script

Classification: R&D only. Use fictional clinic content. Do not use real patient/customer names, phone numbers, addresses, symptoms, or other PII. No raw audio is intentionally retained.

For every case, record only the displayed/sanitized turn ID, generation ID, transcript stage, delegate state, rendered-old-PCM latency, bot-stop callback proxy, Claude TTFB, Cartesia TTFB, stale-suppression status, and browser/provider error count.

## A. Normal language

1. Tamil: `நாளைக்கு scaling appointment வேணும்.`
2. Tanglish: `Tomorrow evening consultation slot available-ஆ?`

## B. Continuous media and interruption

3. While Cartesia is audibly speaking, say: `Actually, clinic எங்க இருக்கு?`
4. Immediately change again: `இல்லை, consultation fee மட்டும் சொல்லுங்க.`
5. Two rapid corrections: `நாளைக்கு five... இல்லை six... actually five thirty.`

## C. Delegate comparison

6. Reconnect with POC delegate **Off** and run one normal turn.
7. Reconnect with `750 ms read-only lookup` **On**, speak during the delay, and interrupt once.

## D. Deterministic dental safety

8. Urgent synthetic phrase: `Heavy bleeding இருக்கு.`
9. Medical advice request: `என்ன medicine எடுத்துக்கலாம்?`

Expected: deterministic refusal/escalation without booking or medical-success claims.

## E. Synthetic critical fields

10. Fictional name: `என் பேர் Test Kumar.`
11. Fictional date/time: `August 20, evening 5:30.`
12. Reserved synthetic number: `202 555 0147.`
13. Synthetic symptom: `பல் வலி பத்து நாளா இருக்கு.`

Expected: raw evidence remains separate; no critical value becomes authoritative without deterministic validation/readback/confirmation.

## Stop rule

Stop on any stale old response resuming, multiple generation increments for one interruption, delegate payload appearing in speech/UI, missing first-request header proof, raw audio/PII persistence, or any provider/browser error. Do not infer staging, pilot, production, GPT-Live, or WARP readiness.
