"""Serve the Pipecat SmallWebRTC runner and the Fonely browser client."""

import importlib.util
import sys
from pathlib import Path

from fastapi.responses import FileResponse
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


app.mount("/assets", StaticFiles(directory=CLIENT_DIST / "assets"), name="voice-lab-assets")

# The Pipecat runner discovers this symbol from __main__.
from pipeline import bot  # noqa: E402,F401


if __name__ == "__main__":
    main()
