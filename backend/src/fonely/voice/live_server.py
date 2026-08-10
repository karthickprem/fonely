"""Live booking server: real LLM + real TTS + real PostgreSQL.

Text-over-WebSocket: browser sends Tamil/Tanglish text, real Anthropic
LLM generates responses, real Cartesia TTS produces Tamil audio,
AppointmentServiceCommandPort commits to real PostgreSQL.

Launch: python -m fonely.voice.live_server
URL: http://localhost:8766
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timezone
from typing import Any

import anthropic
import httpx

from .admission import AdmissionController
from .backend_ports import AppointmentServiceCommandPort, AvailabilityServiceAdapter, build_actor_context
from .config import VoiceSessionConfig
from .context import AvailabilityQuery, AvailableSlot, DayAvailability, TrustedClock
from .runtime import PipelineRuntime

logger = logging.getLogger("fonely.voice.live_server")

LIVE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Fonely Live Booking</title>
<style>
body{font-family:system-ui;max-width:700px;margin:40px auto;padding:0 20px;background:#0a1628;color:#e0e0e0}
h1{color:#00ff88;font-size:1.4em}
.status{background:#0d2137;padding:8px 12px;border-radius:6px;margin-bottom:12px;font-size:0.85em;color:#88ccff}
#chat{border:1px solid #1a3a5c;border-radius:8px;padding:16px;height:400px;overflow-y:auto;background:#0d1a2e;margin-bottom:12px}
.turn{margin:8px 0;padding:8px 12px;border-radius:6px}
.caller{background:#1a2d4e;text-align:right}
.agent{background:#0d2137;border:1px solid #1a3a5c}
.committed{background:#0a2e1a;border:1px solid #1a6633}
.meta{font-size:0.75em;color:#668899;margin-top:4px}
#input-row{display:flex;gap:8px}
#msg{flex:1;padding:10px;border:1px solid #1a3a5c;border-radius:6px;background:#0d1a2e;color:#e0e0e0;font-size:1em}
button{padding:10px 20px;border:none;border-radius:6px;background:#00ff88;color:#000;font-weight:bold;cursor:pointer}
button:hover{background:#00cc6a}
audio{width:100%;margin-top:4px}
.badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:0.7em;margin-left:4px}
.badge-committed{background:#0a2e1a;color:#00ff88}
.badge-blocked{background:#2e0a0a;color:#ff6666}
</style></head><body>
<h1>Fonely Live Booking — Real LLM + TTS + PostgreSQL</h1>
<div class="status">Real Anthropic Claude • Real Cartesia Tamil TTS • Real PostgreSQL appointments</div>
<div id="chat"></div>
<div id="input-row"><input id="msg" placeholder="Tamil/Tanglish: appointment book pannanum..." autofocus>
<button onclick="send()">Send</button></div>
<script>
const ws=new WebSocket(`ws://${location.host}/ws`);const chat=document.getElementById('chat');
const inp=document.getElementById('msg');let terminal=false;
ws.onmessage=e=>{const d=JSON.parse(e.data);
if(d.type==='greeting'){add('agent',d.text,'greeting');if(d.audio){playAudio(d.audio);}}
else if(d.type==='turn_result'){
const cls=d.committed?'committed':(d.allowed?'agent':'agent');
const badge=d.committed?'<span class="badge badge-committed">COMMITTED ✓</span>':
(d.allowed?'':'<span class="badge badge-blocked">BLOCKED</span>');
let extra='';
if(d.receipt_facts){extra='<div class="meta" style="color:#00ff88">'+
'DB: '+d.receipt_facts.service_name+', '+d.receipt_facts.resource_name+
', '+d.receipt_facts.start_at+'</div>';}
add(cls,d.response+badge+extra,
'turn:'+d.turn+' | speech:'+d.speech_class);
if(d.audio){playAudio(d.audio);}
if(d.terminal){terminal=true;inp.disabled=true;inp.placeholder='Booking complete';}
}};
ws.onclose=()=>add('agent','Connection closed','');
function add(cls,html,meta){chat.innerHTML+='<div class="turn '+cls+'">'+html+(meta?'<div class="meta">'+meta+'</div>':'')+'</div>';chat.scrollTop=chat.scrollHeight;}
function playAudio(b64){try{const bytes=atob(b64);const arr=new Uint8Array(bytes.length);for(let i=0;i<bytes.length;i++)arr[i]=bytes.charCodeAt(i);const blob=new Blob([arr],{type:'audio/wav'});const url=URL.createObjectURL(blob);const a=new Audio(url);a.play().catch(()=>{});}catch(e){}}
function send(){if(terminal)return;const t=inp.value.trim();if(!t)return;add('caller',t,'');ws.send(JSON.stringify({text:t}));inp.value='';}
inp.onkeydown=e=>{if(e.key==='Enter')send();};
</script></body></html>"""


class RealLLM:
    """Real Anthropic LLM via AMD gateway."""

    def __init__(self):
        self._client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "dummy"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://llm-api.amd.com/Unified"),
            default_headers=self._parse_headers(),
        )

    def _parse_headers(self) -> dict[str, str]:
        raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        headers = {}
        for line in raw.split("\n"):
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        return headers

    async def generate(self, system: str, messages: list[dict]) -> str:
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model="claude-opus-4-6",
                max_tokens=300,
                system=system,
                messages=messages,
            ),
        )
        return msg.content[0].text

    async def close(self):
        pass


class RealTTS:
    """Real Cartesia TTS with Kavitha Tamil voice."""

    def __init__(self):
        self._api_key = os.environ.get("CARTESIA_API_KEY", "")
        self._voice_id = os.environ.get("CARTESIA_VOICE_ID", "")

    async def synthesize(self, text: str) -> bytes:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "X-API-Key": self._api_key,
                    "Cartesia-Version": "2024-06-10",
                    "Content-Type": "application/json",
                },
                json={
                    "model_id": "sonic-3.5",
                    "transcript": text,
                    "voice": {"mode": "id", "id": self._voice_id},
                    "output_format": {
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                    "language": "ta",
                },
            )
            if r.status_code != 200:
                logger.error("tts_error", extra={"status": r.status_code, "body": r.text[:200]})
                return b""
            return _pcm_to_wav(r.content, sample_rate=24000)

    async def close(self):
        pass


class TextSTT:
    """Text pass-through acting as STT."""

    async def transcribe(self, audio: bytes) -> str:
        return audio.decode("utf-8", errors="replace")

    async def close(self):
        pass


def _pcm_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """Wrap raw PCM in a WAV header for browser playback."""
    import struct

    channels = 1
    bits_per_sample = 16
    data_size = len(pcm)
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm


def _load_env():
    """Load credentials from .env and settings."""
    env_path = "/scratch/karthick/fonely/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)

    settings_path = "/scratch/karthick/.claude/settings.json"
    if os.path.exists(settings_path):
        import json as json_mod

        with open(settings_path) as f:
            data = json_mod.load(f)
        env = data.get("env", {})
        for k, v in env.items():
            os.environ.setdefault(k, v)


def create_live_app():
    """Create FastAPI app with real LLM + TTS + PostgreSQL."""
    _load_env()

    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="Fonely Live Booking")
    admission = AdmissionController(max_per_tenant=3, max_global=5)
    session_counter = 0

    class LiveAvailability:
        async def query_day_availability(self, q: AvailabilityQuery) -> DayAvailability:
            return DayAvailability(
                business_date=q.target_date,
                day_of_week=q.target_date.strftime("%A").lower(),
                is_operating_day=True,
                is_exception_day=False,
                operating_hours=((time(10, 0), time(13, 0)), (time(17, 0), time(20, 30))),
                available_slots=(
                    AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "scaling"),
                    AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
                ),
            )

    @asynccontextmanager
    async def session_factory():
        from fonely.core.database import async_session

        async with async_session() as session:
            yield session

    def validation_factory(session):
        from fonely.api.internal.validation import InternalValidationPort

        return InternalValidationPort(session)

    @app.get("/")
    async def index():
        return HTMLResponse(LIVE_HTML)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "live", "sessions": admission.stats()}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        nonlocal session_counter
        await websocket.accept()
        session_counter += 1
        sid = f"live-{session_counter}"

        decision = admission.try_admit("live-tenant")
        if not decision.admitted:
            await websocket.send_json({"type": "error", "message": "capacity_exceeded"})
            await websocket.close()
            return

        actor = build_actor_context(
            business_id=1,
            phone="+919000000000",
            session_id=sid,
        )

        command_port = AppointmentServiceCommandPort(
            actor=actor,
            session_factory=session_factory,
            validation_factory=validation_factory,
            business_timezone="Asia/Kolkata",
            conversation_id=sid,
        )

        clock = TrustedClock.from_now("Asia/Kolkata")
        config = VoiceSessionConfig(session_id=sid, business_id=1)

        tts = RealTTS()
        runtime = PipelineRuntime(
            config,
            clock=clock,
            business_name="Smile Dental Clinic",
            business_context="Dr. Priya: Mon-Sat, scaling/consultation/root canal. Consultation ₹300, scaling ₹800.",
            business_timezone="Asia/Kolkata",
            stt=TextSTT(),
            llm=RealLLM(),
            tts=tts,
            availability_port=LiveAvailability(),
            command_port=command_port,
            session_mode="live",
        )

        try:
            await runtime.initialize()

            from .prompts import build_greeting

            greeting = build_greeting("Smile Dental Clinic")
            greeting_audio = await tts.synthesize(greeting)
            greeting_b64 = base64.b64encode(greeting_audio).decode() if greeting_audio else ""
            await websocket.send_json({"type": "greeting", "text": greeting, "audio": greeting_b64})

            while True:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=300)
                except asyncio.TimeoutError:
                    break

                msg = json.loads(data)
                text = msg.get("text", "").strip()
                if not text:
                    continue

                result = await runtime.process_turn(text.encode("utf-8"))

                response_text = result.response_text
                audio_b64 = ""
                if result.response_audio:
                    audio_b64 = base64.b64encode(result.response_audio).decode()

                receipt_facts = None
                if result.commit_receipt and result.commit_receipt.facts:
                    receipt_facts = result.commit_receipt.facts

                await websocket.send_json({
                    "type": "turn_result",
                    "turn": result.turn_number,
                    "response": response_text,
                    "audio": audio_b64,
                    "speech_class": result.speech_class,
                    "allowed": result.allowed,
                    "terminal": result.terminal,
                    "terminal_reason": result.terminal_reason,
                    "committed": result.commit_receipt is not None,
                    "receipt_facts": receipt_facts,
                    "availability_queried": result.availability_queried,
                })

                if result.terminal:
                    break

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("live_session_error", extra={"session": sid, "error": str(exc)}, exc_info=True)
        finally:
            await runtime.close("live_end")
            admission.release("live-tenant")

    return app


def main():
    import uvicorn

    _load_env()
    app = create_live_app()
    port = int(os.environ.get("LIVE_PORT", "8766"))
    print(f"\n  Fonely Live Booking: http://localhost:{port}\n")
    print("  Real Anthropic LLM + Real Cartesia TTS + Real PostgreSQL\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
