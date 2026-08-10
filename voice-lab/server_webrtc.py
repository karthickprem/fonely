"""Serve the Pipecat SmallWebRTC runner with both demo and booking pipelines."""

import importlib.util
import sys
import uuid
from pathlib import Path

from fastapi import BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pipecat.runner.run import app, main

LAB_DIR = Path(__file__).resolve().parent
CLIENT_DIST = LAB_DIR / "client" / "dist"
if not (CLIENT_DIST / "index.html").exists():
    raise RuntimeError("Voice client is not built. Start the lab with voice-lab/run.sh.")
sys.path.insert(0, str(LAB_DIR))


@app.get("/voice-lab", include_in_schema=False)
async def voice_lab():
    return FileResponse(CLIENT_DIST / "index.html")


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

# The Pipecat runner discovers this symbol from __main__.
from pipeline import bot  # noqa: E402,F401


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
