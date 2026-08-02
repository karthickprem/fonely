"""Fonely Voice R&D Lab — Python server.

Serves browser UI, bridges browser WebSocket to Sarvam REST APIs.
All API calls are REST (verified working). No Sarvam WebSocket.
"""

import asyncio
import base64
import io
import json
import os
import struct
import time
import wave

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("safety", os.path.join(os.path.dirname(__file__), "safety.py"))
_safety_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_safety_mod)
classify = _safety_mod.classify

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_KEY = os.environ["SARVAM_API_KEY"]
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI()

SYSTEM_PROMPT = """You are the virtual receptionist for Smile Dental Clinic, Aminjikarai, Chennai. Your name is Fonely.

PERSONALITY:
Speak warmly and naturally, like a friendly receptionist at a local Chennai dental clinic. You are helpful, caring, and efficient.

LANGUAGE:
Match the caller's language exactly.
If they speak Tamil, respond in Tamil.
If they speak Tanglish (Tamil + English mix), respond in Tanglish.
If they speak English, respond in Indian English.
Code-switching is natural — "Doctor நாளைக்கு available ah?" is normal.
Use Tamil script for Tamil words.
NEVER use markdown, bullet points, or formatted text.

STYLE:
One or two short spoken sentences per response. Maximum.
Ask only one question at a time.
Use natural acknowledgements: "சரி", "okay", "hmm".
Do not repeat the caller's entire sentence back to them.
Do not say "certainly" or "absolutely" or "I'd be happy to" before every response.
Sound caring when someone mentions pain or worry.

CLINIC INFORMATION:
Smile Dental Clinic, Aminjikarai, Chennai
Doctors:
  Dr. Priya — general, root canal, scaling, extraction (Mon–Sat)
  Dr. Arjun — orthodontics, general (Mon, Wed, Fri)
Hours: 10:00 AM – 1:00 PM, 5:00 PM – 8:30 PM, Monday to Saturday. Sunday closed.
Services:
  General Consultation: ₹300 (20 min)
  Root Canal: ₹3,500–₹5,500 (60 min)
  Scaling/Cleaning: ₹800 (30 min)
  Tooth Extraction: ₹500–₹1,500 (30 min)
  Orthodontics Consultation: ₹500 (30 min)

Available slots tomorrow: 10:00 AM, 11:00 AM, 5:00 PM, 6:30 PM, 7:30 PM
Available slots day after: 10:00 AM, 10:30 AM, 5:00 PM, 6:00 PM, 7:00 PM

SAFETY — NEVER BREAK:
NEVER give medical advice, diagnosis, or treatment recommendations.
NEVER suggest medicines or dosages.
If someone describes symptoms, say you'll connect them with clinic staff.
If emergency, tell them to go to hospital immediately.

BOOKING FLOW:
1. Greet warmly
2. Understand what they need
3. Offer 2–3 available slots
4. Confirm: name, date, time, service
5. Positive closing"""

GREETING = "வணக்கம், Smile Dental Clinic. நான் Fonely, virtual receptionist. எப்படி help பண்ணலாம்?"


def make_wav_header(pcm_bytes: int, sample_rate: int = 16000) -> bytes:
    """Create a WAV header for raw PCM data."""
    h = bytearray(44)
    struct.pack_into("<4sI4s", h, 0, b"RIFF", 36 + pcm_bytes, b"WAVE")
    struct.pack_into("<4sIHHIIHH", h, 12, b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    struct.pack_into("<4sI", h, 36, b"data", pcm_bytes)
    return bytes(h)


async def stt_rest(session: aiohttp.ClientSession, pcm_bytes: bytes) -> dict:
    """Call Sarvam REST STT with PCM audio wrapped in WAV."""
    wav = make_wav_header(len(pcm_bytes)) + pcm_bytes

    form = aiohttp.FormData()
    form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", "saaras:v3")
    form.add_field("language_code", "unknown")

    async with session.post(
        "https://api.sarvam.ai/speech-to-text",
        data=form,
        headers={"api-subscription-key": API_KEY},
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"STT HTTP {resp.status}: {text[:200]}")
        return await resp.json()


async def tts_rest(session: aiohttp.ClientSession, text: str, speaker: str = "kavitha", lang: str = "ta-IN") -> bytes:
    """Call Sarvam REST TTS — returns WAV audio bytes."""
    async with session.post(
        "https://api.sarvam.ai/text-to-speech",
        json={
            "text": text,
            "target_language_code": lang,
            "model": "bulbul:v3",
            "speaker": speaker,
            "speech_sample_rate": 22050,
            "output_audio_codec": "wav",
            "pace": 1.0,
        },
        headers={
            "Content-Type": "application/json",
            "api-subscription-key": API_KEY,
        },
    ) as resp:
        if resp.status != 200:
            text_resp = await resp.text()
            raise RuntimeError(f"TTS HTTP {resp.status}: {text_resp[:200]}")
        data = await resp.json()
        return base64.b64decode(data["audios"][0])


async def llm_chat(session: aiohttp.ClientSession, messages: list) -> str:
    """Call Sarvam LLM — returns assistant response text."""
    async with session.post(
        "https://api.sarvam.ai/v1/chat/completions",
        json={
            "model": "sarvam-105b",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            "max_tokens": 300,
            "temperature": 0.7,
            "reasoning_effort": None,
        },
        headers={
            "Content-Type": "application/json",
            "api-subscription-key": API_KEY,
        },
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"LLM HTTP {resp.status}: {text[:200]}")
        data = await resp.json()
        content = data["choices"][0]["message"].get("content")
        if not content:
            content = "Sorry, one moment. Can you say that again?"
        content = content.strip()
        # Strip markdown artifacts that the LLM sometimes adds
        import re
        content = re.sub(r'\*+', '', content)
        content = re.sub(r'^[-•]\s*', '', content, flags=re.MULTILINE)
        content = content.replace('\n', ' ').strip()
        return content


@app.get("/voice-lab")
async def voice_lab_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.websocket("/ws")
async def voice_lab_ws(ws: WebSocket):
    await ws.accept()
    session_id = os.urandom(4).hex()
    print(f"[{session_id}] Connected")

    messages = []
    speaker = "kavitha"
    lang = "ta-IN"

    def send(msg: dict):
        return ws.send_json(msg)

    async with aiohttp.ClientSession() as http:
        # Send greeting
        await send({"type": "transcript", "role": "assistant", "text": GREETING})
        messages.append({"role": "assistant", "content": GREETING})

        try:
            greeting_wav = await tts_rest(http, GREETING, speaker, lang)
            greeting_b64 = base64.b64encode(greeting_wav).decode()
            await send({"type": "audio", "audio": greeting_b64, "format": "wav"})
            print(f"[{session_id}] Greeting sent: {len(greeting_wav)} bytes WAV")
        except Exception as e:
            print(f"[{session_id}] Greeting TTS error: {e}")

        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)

                if msg["type"] == "audio_complete":
                    pcm = base64.b64decode(msg["audio"])
                    print(f"[{session_id}] STT: received {len(pcm)} bytes")

                    if len(pcm) < 6400:
                        await send({"type": "status", "text": "Too short — speak longer"})
                        continue

                    await send({"type": "status", "text": "Transcribing..."})
                    t0 = time.monotonic()
                    try:
                        result = await stt_rest(http, pcm)
                    except Exception as e:
                        print(f"[{session_id}] STT error: {e}")
                        await send({"type": "status", "text": "STT error — try again"})
                        continue

                    transcript = (result.get("transcript") or "").strip()
                    stt_lang = result.get("language_code", "")
                    stt_ms = int((time.monotonic() - t0) * 1000)
                    print(f"[{session_id}] STT ({stt_ms}ms): \"{transcript}\" [{stt_lang}]")

                    if not transcript:
                        await send({"type": "status", "text": "Could not hear clearly — try again"})
                        continue

                    await send({"type": "stt_result", "transcript": transcript, "language": stt_lang, "latencyMs": stt_ms})
                    await process_turn(http, ws, session_id, messages, transcript, speaker, lang)

                elif msg["type"] == "text":
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    await send({"type": "transcript", "role": "user", "text": text})
                    await process_turn(http, ws, session_id, messages, text, speaker, lang)

                elif msg["type"] == "set_voice":
                    if msg.get("speaker"):
                        speaker = msg["speaker"]
                    if msg.get("language"):
                        lang = msg["language"]
                    await send({"type": "voice_updated", "speaker": speaker, "language": lang})

                elif msg["type"] == "interrupt":
                    await send({"type": "interrupted"})

        except WebSocketDisconnect:
            print(f"[{session_id}] Disconnected")
        except Exception as e:
            print(f"[{session_id}] Error: {e}")


async def process_turn(http, ws, sid, messages, user_text, speaker, lang):
    """Handle one user turn: safety check → LLM → TTS → send audio."""
    t_start = time.monotonic()

    # Deterministic safety check BEFORE LLM
    safety = classify(user_text)
    if safety:
        response = safety["response_ta"] if lang.startswith("ta") else safety["response_en"]
        await ws.send_json({"type": "safety_triggered", "safetyType": safety["type"]})
    else:
        messages.append({"role": "user", "content": user_text})
        await ws.send_json({"type": "status", "text": "Thinking..."})
        t_llm = time.monotonic()
        try:
            response = await llm_chat(http, messages)
        except Exception as e:
            print(f"[{sid}] LLM error: {e}")
            response = "Sorry, one moment. Can you say that again?"
        llm_ms = int((time.monotonic() - t_llm) * 1000)
        print(f"[{sid}] LLM ({llm_ms}ms): \"{response[:80]}\"")
        messages.append({"role": "assistant", "content": response})

    await ws.send_json({"type": "transcript", "role": "assistant", "text": response})

    # TTS
    await ws.send_json({"type": "agent_speaking", "speaking": True})
    await ws.send_json({"type": "status", "text": "Speaking..."})
    t_tts = time.monotonic()
    try:
        wav_bytes = await tts_rest(http, response, speaker, lang)
        tts_ms = int((time.monotonic() - t_tts) * 1000)
        print(f"[{sid}] TTS ({tts_ms}ms): {len(wav_bytes)} bytes WAV")
        wav_b64 = base64.b64encode(wav_bytes).decode()
        await ws.send_json({"type": "audio", "audio": wav_b64, "format": "wav"})
    except Exception as e:
        print(f"[{sid}] TTS error: {e}")
        await ws.send_json({"type": "error", "message": f"TTS failed: {e}"})

    total_ms = int((time.monotonic() - t_start) * 1000)
    await ws.send_json({"type": "agent_speaking", "speaking": False})
    await ws.send_json({"type": "turn_complete", "totalMs": total_ms})
    await ws.send_json({"type": "status", "text": "Ready — click mic or type"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
