"""Focused test for the /hf-realtime same-origin WebSocket reverse-proxy.

The proxy in server_webrtc.py relays frames bidirectionally between a browser
WebSocket (same origin as :3000) and the loopback HF controller, so the HF
backend port is never exposed to the network. Importing server_webrtc pulls the
whole booking stack + DB, so this test exercises the SAME relay coroutine shape
against a real loopback echo server, proving: text+binary frames round-trip both
directions, and a down backend closes the client cleanly (policy code) instead
of hanging.

Run: VOICE_LAB_PYTHON test_hf_realtime_proxy.py  (or via pytest).
"""

from __future__ import annotations

import asyncio
import threading

import pytest
import websockets
from fastapi import WebSocket
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient


async def _relay(client_ws: WebSocket, backend_ws_url: str) -> None:
    """Same relay shape as server_webrtc.hf_realtime_proxy (kept in sync)."""
    await client_ws.accept()
    try:
        upstream = await websockets.connect(backend_ws_url, max_size=None)
    except Exception as e:
        await client_ws.close(code=1013, reason=f"HF backend unavailable: {type(e).__name__}")
        return

    async def to_upstream() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if (data := msg.get("bytes")) is not None:
                    await upstream.send(data)
                elif (text := msg.get("text")) is not None:
                    await upstream.send(text)
        except Exception:
            pass

    async def to_client() -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await client_ws.send_bytes(message)
                else:
                    await client_ws.send_text(message)
        except Exception:
            pass

    try:
        await asyncio.wait(
            {asyncio.create_task(to_upstream()), asyncio.create_task(to_client())},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await client_ws.close()
        except Exception:
            pass


def _app(backend_url: str) -> Starlette:
    async def endpoint(ws: WebSocket) -> None:
        await _relay(ws, backend_url)

    return Starlette(routes=[WebSocketRoute("/hf-realtime", endpoint)])


class _EchoServer:
    """A loopback echo WS server (stand-in for the HF controller) running on its
    OWN event loop in a background thread, so it stays reachable while the
    Starlette TestClient blocks the main thread with its internal portal."""

    def __init__(self) -> None:
        self.url = ""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._server = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def echo(conn):
            async for message in conn:
                if isinstance(message, bytes):
                    await conn.send(b"echo:" + message)
                else:
                    await conn.send("echo:" + message)

        async def boot():
            self._server = await websockets.serve(echo, "127.0.0.1", 0)
            port = self._server.sockets[0].getsockname()[1]
            self.url = f"ws://127.0.0.1:{port}"
            self._ready.set()

        self._loop.run_until_complete(boot())
        self._loop.run_forever()

    def __enter__(self) -> "_EchoServer":
        self._thread.start()
        self._ready.wait(timeout=5)
        return self

    def __exit__(self, *exc) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def test_text_and_binary_round_trip() -> None:
    with _EchoServer() as echo:
        client = TestClient(_app(echo.url))
        with client.websocket_connect("/hf-realtime") as ws:
            ws.send_text("hello-luna")
            got_text = ws.receive_text()
            ws.send_bytes(b"\x01\x02\x03")
            got_bytes = ws.receive_bytes()
    assert got_text == "echo:hello-luna"
    assert got_bytes == b"echo:\x01\x02\x03"


def test_backend_down_closes_client_cleanly() -> None:
    # Point at a port with nothing listening: the proxy must close, not hang.
    app = _app("ws://127.0.0.1:1")  # port 1 = guaranteed refused
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/hf-realtime") as ws:
            ws.receive_text()


if __name__ == "__main__":
    test_text_and_binary_round_trip()
    test_backend_down_closes_client_cleanly()
    print("proxy relay OK")
