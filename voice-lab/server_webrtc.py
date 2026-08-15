"""Serve the Pipecat SmallWebRTC runner with both demo and booking pipelines."""

import contextlib
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Request, WebSocket, WebSocketDisconnect
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


@app.get("/voice-fonely", include_in_schema=False)
async def voice_fonely():
    """The existing Fonely experience: patient call (left) + owner updates (right).

    Unchanged from the historical /voice-test page; relocated here so /voice-test
    can host the two-tab Fonely-vs-HuggingFace comparison with this as tab one.
    """
    return HTMLResponse(SPLIT_PANEL_HTML)


@app.get("/voice-test", include_in_schema=False)
async def voice_test():
    """Two-tab comparison shell: 'Existing Fonely' | 'Hugging Face' (R&D).

    Both tabs are mounted as same-page components inside their own panes. A single
    top-level lifecycle controller guarantees only ONE voice session is ever live:
    switching tabs awaits full teardown (session close + mic tracks stopped + audio
    context closed) of the outgoing pane before the incoming pane may start.
    """
    return HTMLResponse(COMPARISON_SHELL_HTML)


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
    """Reports which pipeline, LLM, and clinic are active.

    Values are DERIVED, not asserted: the model name comes from the same
    constant booking_pipeline builds the LLM from (so it cannot drift from the
    served model), and code_source reports what this lab actually is — a demo
    harness importing the production voice package over a sys.path insert — not
    a claim to BE the production package. The old response hardcoded the model
    as a second literal and labelled itself "(production package)"; both could
    (and did) diverge from reality.
    """
    import production_wiring as pw
    from booking_pipeline import BOOKING_LLM_GATEWAY, BOOKING_LLM_MODEL

    return {
        "pipeline": "booking",
        "llm": {
            "model": BOOKING_LLM_MODEL,
            "gateway": BOOKING_LLM_GATEWAY,
            "provider": "openai-protocol (non-anthropic)",
        },
        "live_context": True,
        # This lab imports the production voice package (fonely.voice) from the
        # dev4-voice-runtime worktree via a sys.path insert. It RUNS that code;
        # it is not itself the shipped package. Reported honestly so a reader is
        # not told the demo IS production.
        "code_source": {
            "role": "lab demo harness",
            "imports": "fonely.voice (dev4-voice-runtime/backend/src, via sys.path)",
        },
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
    <iframe src="/voice-booking" allow="microphone"></iframe>
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


# ---------------------------------------------------------------------------
# Hugging Face comparison tab (isolated R&D — Sarvam STT + Luna LLM + Cartesia
# TTS via the pinned HF speech-to-speech realtime controller).
#
# ISOLATION CONTRACT:
#   * The HF controller runs on a LOOPBACK-only port (127.0.0.1:8765); the
#     browser NEVER sees it. All HF realtime traffic is relayed through the
#     same-origin WebSocket /hf-realtime below, so the user only ever talks to
#     :3000 and cannot point the client at an arbitrary host.
#   * The HF path has NO route to the business database: this module wires only
#     a WS byte-relay, no /api/* booking and no /ws/owner for the HF side.
#   * Synthetic R&D only. The HF LLM may DISCUSS the booking scenario, but it has
#     no tools, cannot mutate state, and must never claim a booking succeeded.
# ---------------------------------------------------------------------------

# Loopback address of the isolated HF realtime controller. Overridable via env
# for a differently-bound dev instance; must stay loopback.
HF_BACKEND_WS = os.environ.get("HF_BACKEND_WS", "ws://127.0.0.1:8765/v1/realtime")

# The HF browser client (pinned demo's s2s-realtime-client.js + integrity-verified
# @openai/agents-realtime UMD) is copied into this lab dir at cutover time.
HF_CLIENT_DIR = LAB_DIR / "hf_client"

# A read-only clinic-booking persona so the HF stack discusses the SAME scenario
# as the Fonely tab for a fair A/B — but it books nothing and has no tools/DB.
HF_READONLY_PERSONA = (
    "You are a friendly dental-clinic receptionist assistant used ONLY for a voice "
    "technology comparison. You may discuss booking a dental appointment and ask for "
    "the service, doctor, date, time, and caller name to demonstrate the conversation. "
    "You do NOT actually book anything, you have no tools, and you must NEVER claim an "
    "appointment was booked, confirmed, or saved. If asked to confirm a booking, explain "
    "this is a read-only technology demo. Keep replies short and natural."
)


@app.get("/hf-static/api/config", include_in_schema=False)
async def hf_client_config(request: Request):
    """Deploy config the HF client fetches (relative 'api/config' from /hf-static/).

    Pins the client to the same-origin /hf-realtime WebSocket proxy so it never
    sees the loopback :8765 backend and no host/port is user-entered. Registered
    BEFORE the /hf-static StaticFiles mount so this dynamic route wins.
    """
    scheme = "wss" if request.url.scheme == "https" else "ws"
    s2s_url = f"{scheme}://{request.url.netloc}/hf-realtime"
    return {
        "allowDirect": True,
        "s2sUrl": s2s_url,
        "lb": False,
        "rtc": False,
        "search": False,
        "startupGreeting": "",
    }


if HF_CLIENT_DIR.is_dir():
    app.mount("/hf-static", StaticFiles(directory=HF_CLIENT_DIR, html=True), name="hf-client")


@app.get("/voice-hf", include_in_schema=False)
async def voice_hf():
    """Serve the HF realtime client page (same-origin, backend pinned to /hf-realtime)."""
    if not (HF_CLIENT_DIR / "index.html").exists():
        return HTMLResponse(
            "<p style='font-family:system-ui;padding:20px;color:#f59e0b'>"
            "HF client not staged yet (hf_client/ missing). Backend wiring is live; "
            "the client bundle is copied in at cutover.</p>",
            status_code=503,
        )
    return FileResponse(HF_CLIENT_DIR / "index.html")


@app.websocket("/hf-realtime")
async def hf_realtime_proxy(client_ws: WebSocket):
    """Same-origin reverse-proxy: browser <-> isolated HF controller (loopback).

    Bidirectional frame relay. The browser connects here (same origin as :3000);
    we open a client connection to the loopback HF backend and shuttle frames both
    ways until either side closes. This is the ONLY bridge to the HF process, so
    the loopback port is never exposed to the network.
    """
    import asyncio

    import websockets

    # WebSocket subprotocol negotiation must be preserved across the proxy. The
    # OpenAI Agents Realtime browser client offers subprotocols (e.g. "realtime",
    # "openai-beta.realtime-v1", and an "openai-insecure-api-key.*" carrying its
    # key) and expects the server to ECHO one back; if the accept carries no
    # subprotocol, the SDK aborts the connection immediately (surfacing as an
    # opaque Event → "[object Event]" in the UI). So: forward the client's offered
    # subprotocols to the upstream HF backend, and echo the upstream's negotiated
    # choice back to the browser. Fall back to the client's first offer when the
    # backend selects none, so the SDK still sees an acknowledged subprotocol.
    offered = [
        p.strip()
        for p in client_ws.headers.get("sec-websocket-protocol", "").split(",")
        if p.strip()
    ]
    try:
        upstream = await websockets.connect(
            HF_BACKEND_WS,
            max_size=None,
            subprotocols=offered or None,
        )
    except Exception as e:
        # Backend not up (e.g. HF controller not launched). Accept then close with
        # a policy code so the client surfaces a real reason rather than hanging.
        await client_ws.accept()
        await client_ws.close(code=1013, reason=f"HF backend unavailable: {type(e).__name__}")
        return

    negotiated = getattr(upstream, "subprotocol", None) or (offered[0] if offered else None)
    await client_ws.accept(subprotocol=negotiated)

    # Lifecycle is critical: the HF backend runs a SINGLE pipeline slot (pool=1),
    # so if this relay leaks — leaving the upstream socket open after the browser
    # goes away — that one slot stays wedged and every later connection is rejected
    # ("all pipeline slots in use"). So when EITHER side ends, we must promptly
    # close the OTHER and fully await the upstream close, freeing the slot at once.
    async def pump_to_upstream() -> None:
        while True:
            msg = await client_ws.receive()
            if msg["type"] == "websocket.disconnect":
                return  # browser went away → let the finally close upstream
            if (data := msg.get("bytes")) is not None:
                await upstream.send(data)
            elif (text := msg.get("text")) is not None:
                await upstream.send(text)

    async def pump_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await client_ws.send_bytes(message)
            else:
                await client_ws.send_text(message)

    to_up = asyncio.create_task(pump_to_upstream())
    to_cl = asyncio.create_task(pump_to_client())
    try:
        done, pending = await asyncio.wait(
            {to_up, to_cl}, return_when=asyncio.FIRST_COMPLETED
        )
        # Cancel the still-running direction and AWAIT it so no pump survives to
        # hold either socket open (a swallowed task would leak the pipeline slot).
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Surface any non-cancellation error from the finished pump for the log,
        # but never let it prevent the closes below.
        for task in done:
            with contextlib.suppress(Exception):
                task.result()
    finally:
        # Close upstream FIRST and wait for it — this is what releases the backend
        # pipeline slot. wait_closed() ensures the close handshake completes before
        # we consider the slot free.
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await upstream.wait_closed()
        # Then close the browser side with a normal code (safe, no secret reason).
        with contextlib.suppress(Exception):
            await client_ws.close(code=1000)


COMPARISON_SHELL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Fonely Voice — Fonely vs Hugging Face</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui;background:#0a1628;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
.tabbar{display:flex;align-items:center;gap:4px;background:#0d2137;border-bottom:1px solid #1a3a5c;padding:0 8px}
.tab{padding:12px 20px;font-size:0.9em;font-weight:700;color:#88a;cursor:pointer;border-bottom:3px solid transparent;background:none;border-top:none;border-left:none;border-right:none;font-family:inherit}
.tab.active{color:#00ff88;border-bottom-color:#00ff88}
.tab:disabled{opacity:0.5;cursor:wait}
.badge{margin-left:auto;font-size:0.68em;color:#f59e0b;font-family:monospace;padding:4px 8px}
.stage{flex:1;position:relative;overflow:hidden}
.pane{position:absolute;inset:0;display:none}
.pane.active{display:block}
.pane iframe{width:100%;height:100%;border:none}
#hf-note{background:#2e1a00;color:#f59e0b;padding:6px 16px;font-size:0.72em;border-bottom:1px solid #3d2800}
#hf-mount{width:100%;height:100%}
.switch-veil{position:absolute;inset:0;background:rgba(10,22,40,0.7);display:none;align-items:center;justify-content:center;color:#88ccff;font-size:0.9em;z-index:5}
.switch-veil.on{display:flex}
</style></head><body>
<div class="tabbar">
  <button class="tab active" id="tab-fonely" data-pane="fonely">Existing Fonely</button>
  <button class="tab" id="tab-hf" data-pane="hf">Hugging Face</button>
  <span class="badge" id="badge">LIVE booking · Fonely stack</span>
</div>
<div class="stage">
  <div class="pane active" id="pane-fonely"></div>
  <div class="pane" id="pane-hf">
    <div id="hf-note">R&amp;D comparison — the LLM may discuss a booking but performs NO booking and touches NO records.</div>
    <div id="hf-mount"></div>
  </div>
  <div class="switch-veil" id="veil">Switching… stopping the other session</div>
</div>
<script type="module">
// Single top-level lifecycle controller. INVARIANT: at most one live voice
// session at any time. Switching AWAITS teardown of the outgoing pane (session
// close + mic tracks stopped + audio context closed) before starting the next.
const FONELY_BADGE = "LIVE booking · Fonely stack";
const HF_BADGE = "Hugging Face controller · Sarvam + Terra + Cartesia · R&D · no booking";

const panes = {
  fonely: { el: document.getElementById('pane-fonely'), tab: document.getElementById('tab-fonely') },
  hf: { el: document.getElementById('pane-hf'), tab: document.getElementById('tab-hf') },
};
const badge = document.getElementById('badge');
const veil = document.getElementById('veil');
let active = null;      // currently live pane key
let switching = false;  // reentrancy guard

// --- Fonely pane: the existing page, wrapped as a same-origin iframe so its
// WebRTC + owner-WS scripts stay fully encapsulated and teardown = drop frame.
function startFonely() {
  const f = document.createElement('iframe');
  f.src = '/voice-fonely';
  f.allow = 'microphone';
  panes.fonely.el.appendChild(f);
}
async function stopFonely() {
  // Removing the iframe tears down its document: getUserMedia tracks stop, the
  // WebRTC peer connection closes, and the owner WebSocket disconnects.
  panes.fonely.el.replaceChildren();
  await new Promise(r => setTimeout(r, 0));
}

// --- HF pane: the pinned HF realtime client mounted as an in-page component,
// backend routed to the same-origin /hf-realtime proxy (never :8765 directly).
// The HF client is a complete multi-file ESM app (main.js + s2s-realtime-client
// + worklets + UMD). Mounting it as a same-origin iframe keeps its modules, CSS,
// AudioContext, and WebSocket fully encapsulated — materially safer than inlining
// its bootstrap here — and lets the parent tear it down deterministically by
// dropping the frame (which stops mic tracks + closes the socket on unload). The
// client is pinned to the same-origin /hf-realtime proxy via /hf-static/api/config.
async function startHf() {
  const f = document.createElement('iframe');
  f.src = '/hf-static/index.html';
  f.allow = 'microphone';
  document.getElementById('hf-mount').appendChild(f);
}
async function stopHf() {
  const mount = document.getElementById('hf-mount');
  const f = mount?.querySelector('iframe');
  // Ask the child to stop cleanly first, then destroy the frame so its document
  // unloads (getUserMedia tracks stop, the realtime WebSocket closes).
  try { f?.contentWindow?.postMessage({ type: 'hf-stop' }, location.origin); } catch (e) { /* ignore */ }
  await new Promise(r => setTimeout(r, 50));
  mount?.replaceChildren();
}

const starters = { fonely: startFonely, hf: startHf };
const stoppers = { fonely: stopFonely, hf: stopHf };

async function activate(key) {
  if (switching || key === active) return;
  switching = true;
  veil.classList.add('on');
  for (const k of Object.keys(panes)) panes[k].tab.disabled = true;
  try {
    // 1) AWAIT full teardown of whatever is live before anything new starts.
    if (active) {
      await stoppers[active]();
      panes[active].el.classList.remove('active');
      panes[active].tab.classList.remove('active');
      active = null;
    }
    // 2) Only now activate + start the incoming pane.
    panes[key].el.classList.add('active');
    panes[key].tab.classList.add('active');
    badge.textContent = key === 'hf' ? HF_BADGE : FONELY_BADGE;
    await starters[key]();
    active = key;
  } finally {
    for (const k of Object.keys(panes)) panes[k].tab.disabled = false;
    veil.classList.remove('on');
    switching = false;
  }
}

for (const k of Object.keys(panes)) {
  panes[k].tab.addEventListener('click', () => activate(k));
}
// Default: the existing Fonely experience (preserves today's behavior).
activate('fonely');
// Safety net: stop any live session if the page is being torn down.
window.addEventListener('pagehide', () => { if (active) stoppers[active](); });
</script></body></html>"""


if __name__ == "__main__":
    main()
