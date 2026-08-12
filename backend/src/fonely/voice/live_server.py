"""Live booking server: real LLM + real TTS + real PostgreSQL.

Supports both text input and browser microphone input.
- Text mode: type Tamil/Tanglish, get real LLM response + TTS audio
- Voice mode: speak into mic, Sarvam STT transcribes, LLM responds, TTS speaks back

Both modes commit real appointments to PostgreSQL through AppointmentServiceCommandPort.
Confirmation speech is derived from the committed receipt, not model intent.

Launch: python -m fonely.voice.live_server
URL: http://localhost:8766
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
from contextlib import asynccontextmanager
from datetime import time
from typing import TYPE_CHECKING

import anthropic
import httpx

from .admission import AdmissionController
from .backend_ports import AppointmentServiceCommandPort, build_actor_context
from .config import VoiceSessionConfig
from .context import AvailabilityQuery, AvailableSlot, DayAvailability, TrustedClock
from .runtime import PipelineRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from sqlalchemy.ext.asyncio import AsyncSession

    from fonely.api.internal.validation import InternalValidationPort

logger = logging.getLogger("fonely.voice.live_server")

LIVE_HTML = (
    "<!DOCTYPE html>\n"
    '<html><head><meta charset="utf-8"><title>Fonely Live Booking</title>\n'
    "<style>\n"
    "*{box-sizing:border-box}\n"
    "body{font-family:system-ui;max-width:750px;margin:40px auto;padding:0"
    " 20px;background:#0a1628;color:#e0e0e0}\n"
    "h1{color:#00ff88;font-size:1.4em;margin-bottom:4px}\n"
    ".subtitle{color:#668899;font-size:0.85em;margin-bottom:16px}\n"
    ".status-bar{background:#0d2137;padding:8px"
    " 12px;border-radius:6px;margin-bottom:12px;font-size:0.8em;color:#88ccff;display:f"
    "lex;justify-content:space-between}\n"
    "#chat{border:1px solid"
    " #1a3a5c;border-radius:8px;padding:16px;height:420px;overflow-y:auto;background:#0"
    "d1a2e;margin-bottom:12px}\n"
    ".turn{margin:8px 0;padding:10px 14px;border-radius:8px;max-width:85%}\n"
    ".caller{background:#1a2d4e;margin-left:auto;text-align:right}\n"
    ".agent{background:#0d2137;border:1px solid #1a3a5c}\n"
    ".committed{background:#0a2e1a;border:2px solid #1a6633}\n"
    ".meta{font-size:0.72em;color:#668899;margin-top:4px}\n"
    ".controls{display:flex;gap:8px;align-items:center}\n"
    "#msg{flex:1;padding:10px 14px;border:1px solid"
    " #1a3a5c;border-radius:8px;background:#0d1a2e;color:#e0e0e0;font-size:1em}\n"
    "button{padding:10px"
    " 20px;border:none;border-radius:8px;font-weight:bold;cursor:pointer;font-size:0.95"
    "em}\n"
    "#sendBtn{background:#00ff88;color:#000}\n"
    "#sendBtn:hover{background:#00cc6a}\n"
    "#micBtn{background:#1a3a5c;color:#88ccff;min-width:120px}\n"
    "#micBtn.recording{background:#cc3333;color:#fff;animation:pulse 1s infinite}\n"
    "@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.7}}\n"
    ".badge{display:inline-block;padding:2px"
    " 8px;border-radius:4px;font-size:0.72em;margin-left:6px;font-weight:bold}\n"
    ".badge-committed{background:#0a2e1a;color:#00ff88;border:1px solid #1a6633}\n"
    ".badge-blocked{background:#2e0a0a;color:#ff6666}\n"
    ".receipt-facts{background:#0a2e1a;border:1px solid"
    " #1a6633;border-radius:6px;padding:8px"
    " 12px;margin-top:6px;font-size:0.85em;color:#00ff88}\n"
    "</style></head><body>\n"
    "<h1>Fonely — Live Voice Booking</h1>\n"
    '<p class="subtitle">Real Claude LLM + Cartesia Tamil TTS + PostgreSQL. Speak Tamil'
    " or type.</p>\n"
    '<div class="status-bar"><span id="status">Ready</span><span id="mode">Text'
    " mode</span></div>\n"
    '<div id="chat"></div>\n'
    '<div class="controls">\n'
    '<input id="msg" placeholder="Type Tamil/Tanglish or click mic..." autofocus>\n'
    '<button id="sendBtn" onclick="sendText()">Send</button>\n'
    '<button id="micBtn" onclick="toggleMic()">🎤 Mic</button>\n'
    "</div>\n"
    "<script>\n"
    "const ws=new WebSocket(`ws://${location.host}/ws`);\n"
    "const chat=document.getElementById('chat');\n"
    "const inp=document.getElementById('msg');\n"
    "const micBtn=document.getElementById('micBtn');\n"
    "const statusEl=document.getElementById('status');\n"
    "const modeEl=document.getElementById('mode');\n"
    "let terminal=false,recording=false,mediaRec=null,audioCtx=null;\n"
    "\n"
    "ws.onopen=()=>{statusEl.textContent='Connected';};\n"
    "ws.onclose=()=>{statusEl.textContent='Disconnected';add('agent','Connection"
    " closed','');};\n"
    "ws.onmessage=e=>{\n"
    "  const d=JSON.parse(e.data);\n"
    "  if(d.type==='greeting'){\n"
    "    add('agent',d.text,'greeting');\n"
    "    if(d.audio)playAudio(d.audio);\n"
    "  }else if(d.type==='turn_result'){\n"
    "    const cls=d.committed?'committed':'agent';\n"
    "    let badge='';\n"
    "    if(d.committed)badge='<span class=\"badge badge-committed\">BOOKED ✓</span>';\n"
    "    else if(!d.allowed)badge='<span class=\"badge badge-blocked\">BLOCKED</span>';\n"
    "    let extra='';\n"
    "    if(d.receipt_facts){\n"
    "      extra='<div class=\"receipt-facts\">PostgreSQL: '+\n"
    "        (d.receipt_facts.service_name||'')+', '+\n"
    "        (d.receipt_facts.resource_name||'')+', '+\n"
    "        (d.receipt_facts.start_at||'')+'</div>';\n"
    "    }\n"
    "    if(d.stt_text)add('caller','🎤 '+d.stt_text,'voice input');\n"
    "    add(cls,d.response+badge+extra,'turn '+d.turn+' | '+d.speech_class);\n"
    "    if(d.audio)playAudio(d.audio);\n"
    "    if(d.terminal){terminal=true;inp.disabled=true;inp.placeholder='Session"
    " ended';statusEl.textContent='Booking complete';}\n"
    "  }\n"
    "};\n"
    "\n"
    "function add(cls,html,meta){\n"
    "  chat.innerHTML+='<div class=\"turn '+cls+'\">'+html+(meta?'<div"
    " class=\"meta\">'+meta+'</div>':'')+'</div>';\n"
    "  chat.scrollTop=chat.scrollHeight;\n"
    "}\n"
    "\n"
    "function playAudio(b64){\n"
    "  try{\n"
    "    const raw=atob(b64);\n"
    "    const arr=new Uint8Array(raw.length);\n"
    "    for(let i=0;i<raw.length;i++)arr[i]=raw.charCodeAt(i);\n"
    "    const blob=new Blob([arr],{type:'audio/wav'});\n"
    "    const a=new Audio(URL.createObjectURL(blob));\n"
    "    a.play().catch(()=>{});\n"
    "  }catch(e){console.error('audio playback error',e);}\n"
    "}\n"
    "\n"
    "function sendText(){\n"
    "  if(terminal)return;\n"
    "  const t=inp.value.trim();if(!t)return;\n"
    "  add('caller',t,'text input');\n"
    "  ws.send(JSON.stringify({type:'text',text:t}));\n"
    "  inp.value='';\n"
    "}\n"
    "inp.onkeydown=e=>{if(e.key==='Enter')sendText();};\n"
    "\n"
    "async function toggleMic(){\n"
    "  if(terminal)return;\n"
    "  if(recording){stopRec();return;}\n"
    "  try{\n"
    "    const stream=await"
    " navigator.mediaDevices.getUserMedia({audio:{sampleRate:16000,channelCount:1,echoC"
    "ancellation:true,noiseSuppression:true}});\n"
    "    mediaRec=new MediaRecorder(stream,{mimeType:'audio/webm;codecs=opus'});\n"
    "    const chunks=[];\n"
    "    mediaRec.ondataavailable=e=>{if(e.data.size>0)chunks.push(e.data);};\n"
    "    mediaRec.onstop=async()=>{\n"
    "      stream.getTracks().forEach(t=>t.stop());\n"
    "      const blob=new Blob(chunks,{type:'audio/webm'});\n"
    "      const buf=await blob.arrayBuffer();\n"
    "      const b64=btoa(String.fromCharCode(...new Uint8Array(buf)));\n"
    "      statusEl.textContent='Processing speech...';\n"
    "      ws.send(JSON.stringify({type:'audio',audio:b64,format:'webm-opus'}));\n"
    "    };\n"
    "    mediaRec.start();\n"
    "    recording=true;\n"
    "    micBtn.textContent='⏹ Stop';\n"
    "    micBtn.classList.add('recording');\n"
    "    modeEl.textContent='Recording...';\n"
    "    statusEl.textContent='Speak now';\n"
    "  }catch(e){\n"
    "    statusEl.textContent='Mic access denied';\n"
    "    console.error(e);\n"
    "  }\n"
    "}\n"
    "\n"
    "function stopRec(){\n"
    "  if(mediaRec&&mediaRec.state==='recording')mediaRec.stop();\n"
    "  recording=false;\n"
    "  micBtn.textContent='🎤 Mic';\n"
    "  micBtn.classList.remove('recording');\n"
    "  modeEl.textContent='Processing...';\n"
    "}\n"
    "</script></body></html>"
)


class RealLLM:
    def __init__(self) -> None:
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

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model="claude-opus-4-6",
                max_tokens=300,
                system=system,
                messages=messages,  # type: ignore[arg-type]
            ),
        )
        block = msg.content[0]
        # Only a text block carries spoken content; anything else yields no text.
        return block.text if block.type == "text" else ""

    async def close(self) -> None:
        pass


class RealTTS:
    def __init__(self) -> None:
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
                logger.error("tts_error", extra={"status": r.status_code})
                return b""
            return _pcm_to_wav(r.content, sample_rate=24000)

    async def close(self) -> None:
        pass


class RealSTT:
    def __init__(self) -> None:
        self._api_key = os.environ.get("SARVAM_API_KEY", "")

    async def transcribe(self, audio: bytes) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": self._api_key},
                files={"file": ("audio.webm", audio, "audio/webm")},
                data={"language_code": "ta-IN", "model": "saaras:v2"},
            )
            if r.status_code != 200:
                logger.error("stt_error", extra={"status": r.status_code, "body": r.text[:200]})
                return ""
            data = r.json()
            transcript = data.get("transcript", "")
            logger.info("stt_result", extra={"transcript": transcript[:80]})
            return transcript if isinstance(transcript, str) else ""

    async def close(self) -> None:
        pass


class TextSTT:
    async def transcribe(self, audio: bytes) -> str:
        return audio.decode("utf-8", errors="replace")

    async def close(self) -> None:
        pass


def _pcm_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
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


def _load_env() -> None:
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
        for k, v in data.get("env", {}).items():
            os.environ.setdefault(k, v)


def create_live_app() -> FastAPI:
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
    async def session_factory() -> AsyncIterator[AsyncSession]:
        from fonely.core.database import async_session

        async with async_session() as session:
            yield session

    def validation_factory(session: AsyncSession) -> InternalValidationPort:
        from fonely.api.internal.validation import InternalValidationPort

        return InternalValidationPort(session)

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(LIVE_HTML)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "mode": "live", "sessions": admission.stats()}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        nonlocal session_counter
        await websocket.accept()
        session_counter += 1
        sid = f"live-{session_counter}"

        decision = admission.try_admit("live-tenant")
        if not decision.admitted:
            await websocket.send_json({"type": "error", "message": "capacity_exceeded"})
            await websocket.close()
            return

        actor = build_actor_context(business_id=1, phone="+919000000000", session_id=sid)
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
        stt = RealSTT()
        runtime = PipelineRuntime(
            config,
            clock=clock,
            business_name="Smile Dental Clinic",
            business_context=(
                "Dr. Priya: Mon-Sat, scaling/consultation/root canal. Consultation ₹300, "
                "scaling ₹800."
            ),
            business_timezone="Asia/Kolkata",
            stt=TextSTT(),
            llm=RealLLM(),
            tts=tts,
            availability_port=LiveAvailability(),
            command_port=command_port,
            session_mode="live",
        )

        transcript: list[dict[str, object]] = []

        try:
            await runtime.initialize()
            from .prompts import build_greeting

            greeting = build_greeting("Smile Dental Clinic")
            greeting_audio = await tts.synthesize(greeting)
            greeting_b64 = base64.b64encode(greeting_audio).decode() if greeting_audio else ""
            await websocket.send_json({"type": "greeting", "text": greeting, "audio": greeting_b64})
            transcript.append({"role": "system", "text": greeting})

            while True:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=300)
                except TimeoutError:
                    break

                msg = json.loads(data)
                msg_type = msg.get("type", "text")

                if msg_type == "audio":
                    audio_b64 = msg.get("audio", "")
                    audio_bytes = base64.b64decode(audio_b64)
                    caller_text = await stt.transcribe(audio_bytes)
                    if not caller_text:
                        await websocket.send_json(
                            {
                                "type": "turn_result",
                                "turn": 0,
                                "response": "குரல் புரியவில்லை, மீண்டும் சொல்லுங்க.",
                                "audio": "",
                                "speech_class": "non_consequential",
                                "allowed": True,
                                "terminal": False,
                                "terminal_reason": "",
                                "committed": False,
                                "receipt_facts": None,
                                "availability_queried": False,
                                "stt_text": "",
                            }
                        )
                        continue
                    input_bytes = caller_text.encode("utf-8")
                    transcript.append({"role": "caller", "text": caller_text, "source": "voice"})
                else:
                    caller_text = msg.get("text", "").strip()
                    if not caller_text:
                        continue
                    input_bytes = caller_text.encode("utf-8")
                    transcript.append({"role": "caller", "text": caller_text, "source": "text"})

                result = await runtime.process_turn(input_bytes)

                response_text = result.response_text
                audio_b64 = ""
                if result.response_audio:
                    audio_b64 = base64.b64encode(result.response_audio).decode()

                receipt_facts = None
                if result.commit_receipt and result.commit_receipt.facts:
                    receipt_facts = result.commit_receipt.facts

                transcript.append(
                    {
                        "role": "agent",
                        "text": response_text,
                        "speech_class": str(result.speech_class),
                        "allowed": result.allowed,
                        "committed": result.commit_receipt is not None,
                        "receipt_facts": receipt_facts,
                    }
                )

                await websocket.send_json(
                    {
                        "type": "turn_result",
                        "turn": result.turn_number,
                        "response": response_text,
                        "audio": audio_b64,
                        "speech_class": str(result.speech_class),
                        "allowed": result.allowed,
                        "terminal": result.terminal,
                        "terminal_reason": result.terminal_reason,
                        "committed": result.commit_receipt is not None,
                        "receipt_facts": receipt_facts,
                        "availability_queried": result.availability_queried,
                        "stt_text": caller_text if msg_type == "audio" else "",
                    }
                )

                if result.terminal:
                    break

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error(
                "live_session_error",
                extra={"session": sid, "error": str(exc)},
                exc_info=True,
            )
        finally:
            await runtime.close("live_end")
            admission.release("live-tenant")
            logger.info(
                "session_transcript",
                extra={
                    "session": sid,
                    "turns": len(transcript),
                    "transcript": json.dumps(transcript, ensure_ascii=False, indent=2),
                },
            )

    return app


def main() -> None:
    import uvicorn

    _load_env()
    app = create_live_app()
    port = int(os.environ.get("LIVE_PORT", "8766"))
    print(f"\n  Fonely Live Booking: http://localhost:{port}")
    print("  Real Anthropic LLM + Cartesia Tamil TTS + PostgreSQL")
    print("  Speak Tamil or type. Mic button for voice input.\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
