"""Per-turn cost-metering proof: run a real booking, meter measured units.

Drives real Cartesia TTS + real Sarvam STT + real LLM gateway for each turn of
a Tamil booking, records RAW measured provider units through CostMeter, forces
one STT retry to show retry-vs-billable separation, runs the reconciliation
assertion, and prints per-turn + per-call raw units.

Money: printed ONLY if a dated PricingBook is supplied. This proof ships an
EMPTY book by default, so it emits units and explicitly says "no dated pricing
source loaded — no cost estimate produced" rather than inventing a price. To
see an estimate, populate PricingBook with sourced/dated entries.

No PII/transcript/audio enters any meter record — the transcript is measured
for length then discarded.
"""
import asyncio
import os
import sys
import time as _time
import io
import wave

sys.path.insert(0, "/scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab")
import httpx
from cost_meter import CostMeter, PricingBook, UNIT_STT_AUDIO_SECONDS, \
    UNIT_LLM_PROMPT_TOKENS, UNIT_LLM_COMPLETION_TOKENS, UNIT_TTS_CHARACTERS

_ENV = {}
for line in open("/scratch/karthick/fonely/.env"):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); _ENV[k] = v

CART_KEY = _ENV["CARTESIA_API_KEY"]
CART_VOICE = _ENV["CARTESIA_VOICE_ID"]
SARVAM_KEY = _ENV["SARVAM_API_KEY"]
TTS_MODEL = "sonic-3.5"
STT_MODEL = "saaras:v3"

BASE = os.environ.get("ANTHROPIC_BASE_URL", "")
SUB = ""
for l in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
    if "Ocp-Apim-Subscription-Key" in l:
        SUB = l.split(":", 1)[1].strip()


def tts_to_wav(text: str) -> bytes:
    r = httpx.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": CART_KEY, "Cartesia-Version": "2024-06-10",
                 "Content-Type": "application/json"},
        json={"model_id": TTS_MODEL, "transcript": text,
              "voice": {"mode": "id", "id": CART_VOICE},
              "output_format": {"container": "wav", "encoding": "pcm_s16le",
                                "sample_rate": 16000}, "language": "ta"},
        timeout=45)
    r.raise_for_status()
    raw = r.content
    di = raw.find(b"data")
    pcm = raw[di + 8:]
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(pcm); w.close()
    return buf.getvalue()


def sarvam_stt(wav_bytes: bytes):
    """Returns (transcript, request_id, processed: bool)."""
    r = httpx.post("https://api.sarvam.ai/speech-to-text",
                   headers={"api-subscription-key": SARVAM_KEY},
                   files={"file": ("a.wav", io.BytesIO(wav_bytes), "audio/wav")},
                   data={"model": STT_MODEL}, timeout=60)
    if r.status_code != 200:
        return "", None, False
    j = r.json()
    return j.get("transcript", ""), j.get("request_id"), True


def call_llm(messages):
    r = httpx.post(f"{BASE}/v1/chat/completions",
                   headers={"Ocp-Apim-Subscription-Key": SUB,
                            "Content-Type": "application/json", "user": "karthick"},
                   json={"model": "gpt-5.6-luna", "max_completion_tokens": 120,
                         "messages": messages}, timeout=60)
    if r.status_code != 200:
        return "", {}, "", False
    j = r.json()
    return (j["choices"][0]["message"]["content"], j.get("usage", {}),
            j.get("model", "gpt-5.6-luna"), True)


async def main():
    print("=" * 74)
    print("PER-TURN COST-METERING PROOF — raw measured units (no PII/transcript)")
    print("=" * 74)
    conv_id = f"costmeter-{int(_time.time())}"
    business_id = 1
    meter = CostMeter(conversation_id=conv_id, business_id=business_id)
    print(f"conversation_id={conv_id} business_id={business_id}")

    turns = ["நாளைக்கு cleaning வேணும்", "மாலை 7:30", "கார்த்திக்", "ஆமா சரி"]
    history = [{"role": "system", "content": "You are a Tamil dental receptionist. Reply in one short sentence."}]

    for i, caller_text in enumerate(turns):
        # 1) TTS the caller's line to audio (this is how we get real audio to STT).
        wav = tts_to_wav(caller_text)
        # The TTS billable unit is CHARACTERS of the transcript we synthesized.
        meter.record_tts(turn_index=i, char_count=len(caller_text),
                         model=TTS_MODEL, attempt=1, provider_processed=True)

        # 2) STT — with a forced retry on the FIRST turn to demonstrate that a
        #    processed retry is billed separately from the successful attempt.
        if i == 0:
            # Attempt 1: a real, provider-processed call (billable) that we then
            # deliberately treat as "needs retry" (e.g. low confidence). The
            # provider DID process it, so it is billable usage.
            _t, _rid, processed1 = sarvam_stt(wav)
            meter.record_stt(turn_index=i, wav_bytes=wav, model=STT_MODEL,
                             attempt=1, provider_processed=processed1, request_id=_rid)
            # Attempt 2: the retry that we actually use downstream.
            stt_text, rid, processed2 = sarvam_stt(wav)
            meter.record_stt(turn_index=i, wav_bytes=wav, model=STT_MODEL,
                             attempt=2, provider_processed=processed2, request_id=rid)
        else:
            stt_text, rid, processed = sarvam_stt(wav)
            meter.record_stt(turn_index=i, wav_bytes=wav, model=STT_MODEL,
                             attempt=1, provider_processed=processed, request_id=rid)

        # 3) LLM on the STT output.
        history.append({"role": "user", "content": stt_text})
        reply, usage, model, ok = call_llm(history)
        meter.record_llm(turn_index=i, usage=usage, model=model,
                         attempt=1, provider_processed=ok)
        history.append({"role": "assistant", "content": reply})
        # NOTE: caller_text/stt_text/reply are NEVER passed to the meter — only
        # their measured lengths / the provider usage block are.
        print(f"  turn {i}: metered (stt retry={'yes' if i==0 else 'no'})")

    # --- Reconciliation assertion -----------------------------------------
    meter.reconcile()
    print("\n  reconciliation: per-turn billable units sum to call totals ✓")

    # --- Raw units --------------------------------------------------------
    print("\n" + "-" * 74)
    print("PER-TURN RAW BILLABLE UNITS (integers; stt in milliseconds of audio)")
    print("-" * 74)
    for turn, kinds in sorted(meter.per_turn_totals().items()):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        print(f"  turn {turn}: {parts}")

    print("\n" + "-" * 74)
    print("PER-CALL RAW BILLABLE TOTALS")
    print("-" * 74)
    for k, v in sorted(meter.call_totals().items()):
        label = f"{v} ms ({v/1000:.3f} s)" if k == UNIT_STT_AUDIO_SECONDS else str(v)
        print(f"  {k}: {label}")

    # --- Retry separation --------------------------------------------------
    retries = meter.retry_records()
    print("\n" + "-" * 74)
    print("RETRY vs BILLABLE SEPARATION")
    print("-" * 74)
    print(f"  total records: {len(meter.records)}  "
          f"billable (provider-processed): {len(meter.billable_records())}  "
          f"retries (attempt>1): {len(retries)}")
    for r in retries:
        print(f"    retry: turn={r.turn_index} {r.provider}/{r.unit_kind} "
              f"count={r.unit_count} processed={r.provider_processed} "
              f"(billable because the provider processed it)")

    # --- Money (only from a dated book) -----------------------------------
    print("\n" + "-" * 74)
    print("COST ESTIMATE")
    print("-" * 74)
    book = PricingBook()  # EMPTY by default — no unsourced price presented as authoritative
    if book.is_empty:
        print("  no dated pricing source loaded — NO cost estimate produced.")
        print("  (units above are measured facts; money requires a PricingBook")
        print("   whose entries each carry a source + retrieved-on date.)")
    else:
        est = meter.estimate_micros(book)
        print("  ESTIMATED (labelled estimate, NOT invoiced):")
        for k, micros in sorted(est.items()):
            shown = "UNPRICED (no dated source)" if micros is None else f"{micros} micro-units"
            print(f"    {k}: {shown}")
    print("\n  invoiced amounts: NONE — no provider API here returns an invoice;")
    print("  estimated and invoiced are separate fields and are never summed.")

    print("\n" + "=" * 74)
    print("RESULT: raw per-turn/per-call usage metered, reconciled, "
          "retry-separated, no PII.")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
