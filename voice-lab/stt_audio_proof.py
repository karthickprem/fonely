"""STT-on-audio proof: real speech → real Sarvam STT → booking → PG row.

The text-in proof never exercised speech recognition. This generates each
booking turn as Tamil audio (Cartesia TTS), transcribes it with real Sarvam
STT (saaras:v3), and feeds the REAL STT OUTPUT — not clean text — through the
production FrameProcessors, asserting the committed row. It reports, per turn,
the clean input vs what STT produced, so mis-recognitions that would break a
real call are visible.

This is the failure the CEO named: a booking that works on typed text and dies
on a mis-recognised doctor name or a transliterated service word.
"""
import asyncio, os, sys, time as _time, wave, io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab")
import httpx
import production_wiring as pw
from live_proof import run_turn, _load_system_prompt, query_appointments

_ENV = {}
for line in open("/scratch/karthick/fonely/.env"):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); _ENV[k] = v

CART_KEY = _ENV["CARTESIA_API_KEY"]
CART_VOICE = _ENV["CARTESIA_VOICE_ID"]
SARVAM_KEY = _ENV["SARVAM_API_KEY"]
TZ = ZoneInfo("Asia/Kolkata")


def tts_to_wav(text: str) -> bytes:
    """Cartesia TTS → 16kHz mono WAV bytes, with a corrected header (Cartesia
    streams a placeholder frame count that reads as 37 hours)."""
    r = httpx.post(
        "https://api.cartesia.ai/tts/bytes",
        headers={"X-API-Key": CART_KEY, "Cartesia-Version": "2024-06-10", "Content-Type": "application/json"},
        json={"model_id": "sonic-3.5", "transcript": text,
              "voice": {"mode": "id", "id": CART_VOICE},
              "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 16000},
              "language": "ta"},
        timeout=45,
    )
    r.raise_for_status()
    raw = r.content
    di = raw.find(b"data")
    pcm = raw[di + 8:]
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(pcm); w.close()
    return buf.getvalue()


def sarvam_stt(wav_bytes: bytes) -> tuple[str, float]:
    """Real Sarvam STT on the audio. Returns (transcript, latency_ms)."""
    t0 = _time.monotonic()
    r = httpx.post(
        "https://api.sarvam.ai/speech-to-text",
        headers={"api-subscription-key": SARVAM_KEY},
        files={"file": ("audio.wav", io.BytesIO(wav_bytes), "audio/wav")},
        data={"model": "saaras:v3"},
        timeout=60,
    )
    ms = (_time.monotonic() - t0) * 1000
    if r.status_code != 200:
        return f"[STT ERROR {r.status_code}]", ms
    return r.json().get("transcript", ""), ms


async def main():
    print("=" * 74)
    print("STT-ON-AUDIO PROOF — Cartesia TTS → real Sarvam STT → booking → PG row")
    print("=" * 74)
    conv_id = f"sttaudio-{int(_time.time())}"
    injector, gate = pw.build_processors(conv_id)
    system = _load_system_prompt()

    # A booking, spoken. Each is TTS'd, STT'd, then the STT OUTPUT drives booking.
    # Use an evening slot unlikely to be taken.
    clean_turns = [
        "நாளைக்கு cleaning வேணும்",
        "மாலை 7:30",
        "கார்த்திக்",
        "ஆமா சரி",
    ]

    tomorrow = (datetime.now(TZ) + timedelta(days=1)).date()
    before = await query_appointments(pw.DEMO_BUSINESS_ID)
    before_id = max([a["id"] for a in before], default=0)

    history = []
    stt_mismatches = []
    stt_latencies = []
    for clean in clean_turns:
        wav = tts_to_wav(clean)
        stt_text, stt_ms = sarvam_stt(wav)
        stt_latencies.append(stt_ms)
        mismatch = stt_text.strip().rstrip(".") != clean.strip()
        if mismatch:
            stt_mismatches.append((clean, stt_text))
        # Feed the REAL STT OUTPUT through the pipeline, not the clean text.
        spoken, ms, raw, lang = await run_turn(injector, gate, system, history, stt_text)
        flag = " ⚠ STT-CHANGED" if mismatch else ""
        print(f"  clean:  {clean}")
        print(f"  STT:    {stt_text}{flag}  [{stt_ms:.0f}ms]")
        print(f"  agent:  {spoken[:110]}")
        print()

    after = await query_appointments(pw.DEMO_BUSINESS_ID, since_id=before_id)
    print("=" * 74)
    print("RESULT")
    print("=" * 74)
    if after:
        a = after[-1]
        local = a["start_at"].astimezone(TZ)
        rd = local.date() == tomorrow
        print(f"  ✓ COMMITTED through real STT: appt #{a['id']} {a['service_name_snapshot']} "
              f"{local.strftime('%m-%d %H:%M')} day={'RIGHT' if rd else 'WRONG'}")
    else:
        print("  ✗ NO row — booking did not survive real STT (this is the finding).")
    print()
    print(f"  STT mismatches (clean vs recognised): {len(stt_mismatches)}")
    for clean, got in stt_mismatches:
        print(f"    '{clean}' → '{got}'")
    if stt_latencies:
        print(f"  STT latency: avg={sum(stt_latencies)/len(stt_latencies):.0f}ms max={max(stt_latencies):.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
