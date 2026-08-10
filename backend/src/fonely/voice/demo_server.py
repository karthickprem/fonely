"""Provider-free browser demo server wired to production PipelineRuntime.

Text-over-WebSocket: browser sends text, runtime processes through
full pipeline (availability, classifier, validator gate, dialogue
state, terminal), browser receives response + turn evidence.

No STT/TTS credentials needed. Feature-gated demo mode.
Launch: python -m fonely.voice.demo_server
URL: http://localhost:8765
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timezone
from pathlib import Path

logger = logging.getLogger("fonely.voice.demo_server")

# Inline minimal HTML — no external build needed
DEMO_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Fonely Voice Demo</title>
<style>
body{font-family:system-ui;max-width:700px;margin:40px auto;padding:0 20px;background:#1a1a2e;color:#e0e0e0}
h1{color:#00d2ff;font-size:1.4em}
#chat{border:1px solid #333;border-radius:8px;padding:16px;height:400px;overflow-y:auto;background:#16213e;margin-bottom:12px}
.turn{margin:8px 0;padding:8px 12px;border-radius:6px}
.caller{background:#0f3460;text-align:right}
.agent{background:#1a1a2e;border:1px solid #333}
.meta{font-size:0.75em;color:#888;margin-top:4px}
.blocked{background:#3d0000;border:1px solid #660000}
.terminal{background:#1a3d00;border:1px solid #336600}
#input-row{display:flex;gap:8px}
#msg{flex:1;padding:10px;border:1px solid #333;border-radius:6px;background:#16213e;color:#e0e0e0;font-size:1em}
button{padding:10px 20px;border:none;border-radius:6px;background:#00d2ff;color:#000;font-weight:bold;cursor:pointer}
button:hover{background:#00b8e6}
.badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:0.7em;margin-left:4px}
.badge-allow{background:#1a3d00;color:#66ff66}
.badge-block{background:#3d0000;color:#ff6666}
.badge-terminal{background:#3d3d00;color:#ffff66}
</style></head><body>
<h1>Fonely Voice Runtime Demo</h1>
<p style="color:#888;font-size:0.85em">Provider-free text demo wired to production PipelineRuntime. Type Tamil, Tanglish, or English.</p>
<div id="chat"></div>
<div id="input-row"><input id="msg" placeholder="Type here... (e.g., இன்னைக்கு doctor free-ஆ?)" autofocus>
<button onclick="send()">Send</button></div>
<script>
const ws=new WebSocket(`ws://${location.host}/ws`);const chat=document.getElementById('chat');
const inp=document.getElementById('msg');let terminal=false;
ws.onmessage=e=>{const d=JSON.parse(e.data);
if(d.type==='greeting'){add('agent',d.text,'greeting');}
else if(d.type==='turn_result'){
const cls=d.allowed?'agent':(d.terminal?'terminal':'blocked');
const badge=d.terminal?'<span class="badge badge-terminal">TERMINAL</span>':
d.allowed?'<span class="badge badge-allow">ALLOW</span>':'<span class="badge badge-block">BLOCK</span>';
add(cls,d.response+badge,
`speech:${d.speech_class} | turn:${d.turn} | avail:${d.availability_queried}`);
if(d.terminal){terminal=true;inp.disabled=true;inp.placeholder='Session ended';}
}else if(d.type==='error'){add('blocked','Error: '+d.message,'');}
};
ws.onclose=()=>add('blocked','Connection closed','');
function add(cls,html,meta){chat.innerHTML+=`<div class="turn ${cls}">${html}${meta?'<div class="meta">'+meta+'</div>':''}</div>`;chat.scrollTop=chat.scrollHeight;}
function send(){if(terminal)return;const t=inp.value.trim();if(!t)return;add('caller',t,'');ws.send(JSON.stringify({text:t}));inp.value='';}
inp.onkeydown=e=>{if(e.key==='Enter')send();};
</script></body></html>"""


def create_demo_app():
    """Create FastAPI app with WebSocket endpoint wired to PipelineRuntime."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse

    from .admission import AdmissionController
    from .config import VoiceSessionConfig
    from .context import (
        AvailabilityQuery,
        AvailableSlot,
        DayAvailability,
        TrustedClock,
    )
    from .runtime import PipelineRuntime

    app = FastAPI(title="Fonely Voice Demo")
    admission = AdmissionController(max_per_tenant=5, max_global=20)
    session_counter = 0

    # Mock availability with real-looking dental data
    def _monday_avail(target_date: date) -> DayAvailability:
        dow = target_date.strftime("%A").lower()
        if dow == "sunday":
            return DayAvailability(
                business_date=target_date, day_of_week=dow,
                is_operating_day=False, is_exception_day=False,
                reason="Sunday closed",
            )
        return DayAvailability(
            business_date=target_date, day_of_week=dow,
            is_operating_day=True, is_exception_day=False,
            operating_hours=((time(10, 0), time(13, 0)), (time(17, 0), time(20, 30))),
            available_slots=(
                AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "consultation"),
                AvailableSlot(1, "Dr. Priya", time(11, 0), time(11, 30), "scaling"),
                AvailableSlot(2, "Dr. Arjun", time(10, 0), time(10, 30), "orthodontics"),
                AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "consultation"),
            ),
        )

    class DemoAvailability:
        async def query_day_availability(self, q: AvailabilityQuery) -> DayAvailability:
            return _monday_avail(q.target_date)

    class TextSTT:
        """Pass-through: text input acts as STT output."""
        async def transcribe(self, audio: bytes) -> str:
            return audio.decode("utf-8", errors="replace")
        async def close(self): pass

    class TextLLM:
        """Simple rule-based LLM for demo without credentials."""
        async def generate(self, system: str, messages: list[dict]) -> str:
            if not messages:
                return ""
            last = messages[-1].get("content", "").lower()

            # Extract availability from system prompt
            if any(w in last for w in ["free", "available", "slot", "இருக்கா", "கிடைக்கும்"]):
                if "Dr. Priya" in system:
                    return "Dr. Priya 10:00, 11:00, 18:30 available. எந்த time?"
                return "Availability data not connected."

            if any(w in last for w in ["fee", "price", "எவ்வளவு", "cost"]):
                return "Consultation ₹300, scaling ₹800."

            if any(w in last for w in ["location", "address", "எங்க", "where"]):
                return "Aminjikarai, Chennai."

            if any(w in last for w in ["book", "appointment", "வேணும்", "பண்ணனும்"]):
                if "demo" in system.lower():
                    return "இது demo — booking process show பண்ணலாம், ஆனா save ஆகாது. என்ன reason-க்காக visit?"
                return "என்ன reason-க்காக visit?"

            if any(w in last for w in ["scaling", "cleaning", "root canal", "checkup", "pain", "வலி"]):
                return "எந்த date-ல வரணும்?"

            if any(w in last for w in ["bye", "thanks", "நன்றி", "போறேன்"]):
                return "வணக்கம், நன்றி!"

            return "எப்படி help பண்ணலாம்?"

        async def close(self): pass

    class TextTTS:
        """Pass-through: returns text as bytes for display."""
        async def synthesize(self, text: str) -> bytes:
            return text.encode("utf-8")
        async def close(self): pass

    @app.get("/")
    async def index():
        return HTMLResponse(DEMO_HTML)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "demo", "sessions": admission.stats()}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        nonlocal session_counter
        await websocket.accept()
        session_counter += 1
        sid = f"demo-{session_counter}"

        decision = admission.try_admit("demo-tenant")
        if not decision.admitted:
            await websocket.send_json({"type": "error", "message": "capacity_exceeded"})
            await websocket.close()
            return

        clock = TrustedClock.from_now("Asia/Kolkata")
        config = VoiceSessionConfig(session_id=sid, business_id=1)

        runtime = PipelineRuntime(
            config,
            clock=clock,
            business_name="Smile Dental Clinic",
            business_context="Dr. Priya: Mon-Sat, general/root canal/scaling. Dr. Arjun: Mon/Wed/Fri, orthodontics. Consultation ₹300, scaling ₹800.",
            business_timezone="Asia/Kolkata",
            stt=TextSTT(),
            llm=TextLLM(),
            tts=TextTTS(),
            availability_port=DemoAvailability(),
            session_mode="demo",
        )

        try:
            await runtime.initialize()

            # Send greeting
            from .prompts import build_greeting
            greeting = build_greeting("Smile Dental Clinic")
            await websocket.send_json({"type": "greeting", "text": greeting})

            while True:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=300)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "error", "message": "idle_timeout"})
                    break

                msg = json.loads(data)
                text = msg.get("text", "").strip()
                if not text:
                    continue

                result = await runtime.process_turn(text.encode("utf-8"))

                response_text = result.response_text
                if result.response_audio:
                    response_text = result.response_audio.decode("utf-8", errors="replace")

                await websocket.send_json({
                    "type": "turn_result",
                    "turn": result.turn_number,
                    "response": response_text,
                    "speech_class": result.speech_class,
                    "allowed": result.allowed,
                    "terminal": result.terminal,
                    "terminal_reason": result.terminal_reason,
                    "availability_queried": result.availability_queried,
                    "commit_evidence": result.commit_receipt is not None,
                })

                if result.terminal:
                    from .dialogue import get_terminal_response
                    terminal = get_terminal_response(result.terminal_reason, "ta-Latn")
                    if terminal:
                        await websocket.send_json({"type": "turn_result", "turn": 0, "response": terminal, "speech_class": "non_consequential", "allowed": True, "terminal": True, "terminal_reason": result.terminal_reason, "availability_queried": False, "commit_evidence": False})
                    break

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("demo_session_error", extra={"session": sid, "error": type(exc).__name__})
        finally:
            await runtime.close("demo_end")
            admission.release("demo-tenant")


    return app


def main():
    import uvicorn
    app = create_demo_app()
    port = int(os.environ.get("DEMO_PORT", "8765"))
    print(f"\n  Fonely Voice Demo: http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
