"""Serve the Pipecat SmallWebRTC runner with both demo and booking pipelines."""

import importlib.util
import json
import sys
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pipecat.runner.run import app, main

LAB_DIR = Path(__file__).resolve().parent
CLIENT_DIST = LAB_DIR / "client" / "dist"
if not (CLIENT_DIST / "index.html").exists():
    raise RuntimeError("Voice client is not built. Start the lab with voice-lab/run.sh.")
sys.path.insert(0, str(LAB_DIR))

# Production wiring: same DB (fonely_dev4), same doctor bridge, same clinic
# context the agent pipeline uses. Owner panel and agent share one source.
from production_wiring import BRIDGE, clinic_context_text as _prod_clinic_context

async def get_clinic_context() -> str:
    return await _prod_clinic_context()

# Owner commands (schedule changes) still go through the lab db_backend, which
# writes real schedule_exceptions — but it must target fonely_dev4 like the
# rest. production_wiring sets DATABASE_URL before db_backend loads.
from db_backend import process_owner_command


@app.get("/voice-lab", include_in_schema=False)
async def voice_lab():
    return FileResponse(CLIENT_DIST / "index.html")


@app.get("/voice-test", include_in_schema=False)
async def voice_test():
    """Split-panel test: patient call (left) + owner updates (right)."""
    return HTMLResponse(SPLIT_PANEL_HTML)


@app.websocket("/ws/owner")
async def owner_ws(websocket: WebSocket):
    """Owner/Doctor panel — two-way communication with agent."""
    await websocket.accept()
    BRIDGE.register_ws(websocket)
    try:
        context = await get_clinic_context()
        await websocket.send_json({
            "type": "state",
            "context": context,
            "updates": [],
            "pending_queries": BRIDGE.pending_queries,
        })
    except Exception as e:
        await websocket.send_json({"type": "state", "context": f"DB error: {e}", "updates": []})
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            # Doctor responding to an agent question
            if msg.get("type") == "doctor_response":
                query_id = msg.get("query_id")
                response = msg.get("response", "").strip()
                if query_id is not None and response:
                    BRIDGE.doctor_responds(query_id, response)
                    await websocket.send_json({
                        "type": "response_sent",
                        "query_id": query_id,
                        "response": response,
                    })
                continue

            # Owner command (schedule update, leave, etc.)
            text = msg.get("text", "").strip()
            if not text:
                continue
            try:
                result = await process_owner_command(text)
                context = result.get("context", "")
                await websocket.send_json({
                    "type": "update_applied",
                    "input": text,
                    "result": "applied" if result["success"] else "rejected",
                    "message": result["message"],
                    "slots_before": [],
                    "slots_after": [],
                    "context": context,
                    "updates": [],
                })
            except Exception as e:
                await websocket.send_json({
                    "type": "update_applied",
                    "input": text,
                    "result": "error",
                    "message": str(e),
                    "slots_before": [],
                    "slots_after": [],
                    "context": "",
                    "updates": [],
                })
    except WebSocketDisconnect:
        BRIDGE.unregister_ws(websocket)


@app.post("/api/clinic-reset")
async def reset_clinic():
    """Clear today/tomorrow schedule exceptions in the demo DB (test only).
    Removes any owner-set leave/closure so the clinic is back to its base
    operating schedule."""
    from db_backend import process_owner_command
    r1 = await process_owner_command("today open")
    r2 = await process_owner_command("tomorrow open")
    return {"reset": True, "today": r1.get("message"), "tomorrow": r2.get("message")}


@app.get("/api/clinic-context")
async def api_clinic_context():
    """Current clinic context from PostgreSQL."""
    try:
        context = await get_clinic_context()
        return {"context": context, "updates": []}
    except Exception as e:
        return {"context": f"DB error: {e}", "updates": []}


@app.get("/api/pipeline-info")
async def pipeline_info():
    """Confirms which pipeline, LLM, and clinic are active — routing guard.
    Reads the real demo clinic from the production package."""
    import production_wiring as pw
    return {
        "pipeline": "booking",
        "llm": "gpt-5.6-luna",
        "live_context": True,
        "code_source": "backend/src/fonely/voice (production package)",
        "clinic_profile": {
            "business_id": pw.DEMO_BUSINESS_ID,
            "db": "fonely_dev4",
            "synthetic": True,
        },
    }


SPLIT_PANEL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Fonely Voice Test — Owner + Patient</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#0a1628;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
h1{font-size:1.2em;color:#00ff88;padding:12px 16px;border-bottom:1px solid #1a3a5c}
.warn{background:#2e1a00;color:#f59e0b;padding:6px 16px;font-size:0.75em;border-bottom:1px solid #3d2800}
.panels{display:flex;flex:1;overflow:hidden}
.panel{flex:1;display:flex;flex-direction:column;border-right:1px solid #1a3a5c}
.panel:last-child{border-right:none}
.panel-header{padding:10px 14px;background:#0d2137;border-bottom:1px solid #1a3a5c;font-size:0.85em;font-weight:700}
.panel-header.patient{color:#3b9eff}
.panel-header.owner{color:#f59e0b}
.content{flex:1;overflow-y:auto;padding:12px}
.context-box{background:#0d1a2e;border:1px solid #1a3a5c;border-radius:6px;padding:10px;font-size:0.72em;margin-bottom:10px;white-space:pre-wrap;font-family:monospace;color:#88ccff;max-height:200px;overflow-y:auto}
.update-log{font-size:0.72em;color:#668899}
.update-entry{padding:3px 0;border-bottom:1px solid #0d1a2e}
.update-time{color:#f59e0b;font-weight:600}
.msg{margin:6px 0;padding:8px 10px;border-radius:6px;font-size:0.82em}
.msg.owner-in{background:#2e1a00;border:1px solid #3d2800;text-align:right}
.msg.owner-out{background:#0d2137;border:1px solid #1a3a5c}
.msg.system{background:#0a2e1a;color:#22c55e;font-size:0.72em}
.msg.agent-q{background:#1a0a2e;border:1px solid #4a2a7a;color:#a78bfa;font-size:0.82em}
.msg.agent-q .q-label{font-weight:700;font-size:0.75em;color:#7c3aed;margin-bottom:4px}
.reply-row{display:flex;gap:4px;margin-top:6px}
.reply-row input{flex:1;padding:5px 8px;background:#0d1a2e;border:1px solid #4a2a7a;border-radius:4px;color:#e0e0e0;font-size:0.8em}
.reply-row button{padding:5px 10px;background:#7c3aed;color:#fff;border:none;border-radius:4px;font-size:0.75em;font-weight:700;cursor:pointer}
.input-row{display:flex;gap:6px;padding:10px;border-top:1px solid #1a3a5c}
.input-row input{flex:1;padding:8px 10px;background:#0d1a2e;border:1px solid #1a3a5c;border-radius:6px;color:#e0e0e0;font-size:0.85em}
.input-row button{padding:8px 16px;border:none;border-radius:6px;font-weight:700;cursor:pointer;font-size:0.85em}
.btn-owner{background:#f59e0b;color:#000}
.btn-patient{background:#3b9eff;color:#000}
iframe{width:100%;height:100%;border:none}
</style></head><body>
<h1>Fonely Voice Test — Owner Updates + Patient Call</h1>
<div class="warn">⚠ LIVE PostgreSQL — Real booking. Doctor must confirm availability before agent offers slots. Two-way: agent asks doctor when it doesn't know.</div>
<div id="pipeline-id" style="background:#0a2e1a;color:#22c55e;padding:4px 16px;font-size:0.7em;font-family:monospace;border-bottom:1px solid #1a3a5c">Pipeline: loading...</div>
<div class="panels">
  <div class="panel">
    <div class="panel-header patient">🎤 Patient Call — Booking Pipeline (GPT-5.6 Luna + Live Clinic Context)</div>
    <iframe src="/voice-booking"></iframe>
  </div>
  <div class="panel">
    <div class="panel-header owner">👨‍⚕️ Owner / Doctor Updates</div>
    <div class="content" id="owner-content">
      <div class="context-box" id="clinic-ctx">Loading clinic context...</div>
      <div id="owner-msgs"></div>
      <div class="update-log" id="update-log"></div>
    </div>
    <div class="input-row">
      <input id="owner-input" placeholder="E.g.: today leave, today open, scaling Rs500...">
      <button class="btn-owner" onclick="sendOwnerUpdate()">Send</button>
    </div>
  </div>
</div>
<script>
const ows=new WebSocket(`ws://${location.host}/ws/owner`);
const msgs=document.getElementById('owner-msgs');
const ctx=document.getElementById('clinic-ctx');
const log=document.getElementById('update-log');
const inp=document.getElementById('owner-input');

ows.onmessage=e=>{
  const d=JSON.parse(e.data);
  if(d.context)ctx.textContent=d.context;

  // Agent asking doctor a question
  if(d.type==='agent_question'){
    const qid=d.query_id;
    msgs.innerHTML+=`<div class="msg agent-q" id="q-${qid}">
      <div class="q-label">🤖 Agent asks you:</div>
      ${d.question}
      ${d.patient_context?`<div style="font-size:0.75em;color:#668899;margin-top:4px">Patient context: ${d.patient_context}</div>`:''}
      <div class="reply-row">
        <input id="reply-${qid}" placeholder="Reply to agent...">
        <button onclick="replyToAgent(${qid})">Reply</button>
      </div>
    </div>`;
    msgs.scrollTop=msgs.scrollHeight;
    // Play a notification sound effect via CSS animation
    document.getElementById('q-'+qid).style.animation='pulse 0.5s ease-in-out 3';
    return;
  }

  // Doctor's response confirmed
  if(d.type==='response_sent'){
    const qel=document.getElementById('q-'+d.query_id);
    if(qel){
      const rrow=qel.querySelector('.reply-row');
      if(rrow) rrow.innerHTML=`<span style="color:#22c55e;font-size:0.8em">✓ Replied: ${d.response}</span>`;
    }
    return;
  }

  if(d.type==='update_applied'){
    msgs.innerHTML+=`<div class="msg owner-in">${d.input}</div>`;
    const color=d.result==='applied'?'#22c55e':d.result==='rejected'?'#ef4444':'#888';
    const icon=d.result==='applied'?'✓':d.result==='rejected'?'✗':'○';
    msgs.innerHTML+=`<div class="msg system" style="color:${color}">${icon} [${d.result}] ${d.message}</div>`;
  }
  msgs.scrollTop=msgs.scrollHeight;
};
ows.onclose=()=>msgs.innerHTML+='<div class="msg system">Connection closed</div>';

function sendOwnerUpdate(){
  const t=inp.value.trim();if(!t)return;
  ows.send(JSON.stringify({text:t}));
  inp.value='';
}
function replyToAgent(qid){
  const el=document.getElementById('reply-'+qid);
  if(!el)return;
  const t=el.value.trim();if(!t)return;
  ows.send(JSON.stringify({type:'doctor_response',query_id:qid,response:t}));
}
inp.onkeydown=e=>{if(e.key==='Enter')sendOwnerUpdate();};
fetch('/api/pipeline-info').then(r=>r.json()).then(d=>{
  document.getElementById('pipeline-id').textContent=
    'Pipeline: '+d.pipeline+' | LLM: '+d.llm+' | DB: PostgreSQL';
  document.getElementById('pipeline-id').style.color='#22c55e';
});
</script></body></html>"""


@app.get("/voice-booking", include_in_schema=False)
async def voice_booking():
    """Serve the booking-enabled voice interface.

    Reuses the same WebRTC client but connects to the booking pipeline.
    The client JS is patched to hit /api/booking-offer instead of /api/offer.
    """
    html = (CLIENT_DIST / "index.html").read_text()
    html = html.replace(
        "<title>Fonely Continuous Voice Lab</title>",
        "<title>Fonely Live Booking</title>",
    )
    html = html.replace(
        "Fonely Voice Lab",
        "Fonely Live Booking",
    )
    html = html.replace(
        "Synthetic clinic · R&amp;D only",
        "LIVE · Real booking · PostgreSQL",
    )
    html = html.replace(
        "Continuous Tamil · Tanglish · Indian English",
        "Speak Tamil · Book an appointment",
    )
    html = html.replace("/api/offer", "/api/booking-offer")
    return HTMLResponse(html)


app.mount("/assets", StaticFiles(directory=CLIENT_DIST / "assets"), name="voice-lab-assets")

# The Pipecat runner discovers `bot` from __main__ for /api/offer.
# Use the booking pipeline (with live clinic context) as the default.
from booking_pipeline import bot  # noqa: E402,F401


# Add booking-specific offer endpoint
@app.post("/api/booking-offer")
async def booking_offer(request: dict, background_tasks: BackgroundTasks):
    """Handle WebRTC offer for the booking pipeline."""
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )
    from pipecat.runner.run import SmallWebRTCRunnerArguments

    import booking_pipeline

    handler = SmallWebRTCRequestHandler(esp32_mode=False, host="0.0.0.0")
    session_id = str(uuid.uuid4())

    webrtc_request = SmallWebRTCRequest(
        sdp=request["sdp"],
        type=request["type"],
        pc_id=request.get("pc_id"),
        restart_pc=request.get("restart_pc"),
        request_data=request.get("request_data") or request.get("requestData"),
    )

    async def webrtc_connection_callback(connection: SmallWebRTCConnection):
        runner_args = SmallWebRTCRunnerArguments(
            webrtc_connection=connection,
            body=webrtc_request.request_data,
            session_id=session_id,
        )
        background_tasks.add_task(booking_pipeline.bot, runner_args)

    answer = await handler.handle_web_request(
        request=webrtc_request,
        webrtc_connection_callback=webrtc_connection_callback,
    )
    return answer


if __name__ == "__main__":
    main()
