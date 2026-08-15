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

import contextlib
import time

import pytest
import websockets
from fastapi import WebSocket
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient


def _wait_until(pred, timeout: float = 3.0) -> None:
    """Poll a predicate until true or timeout (for cross-thread slot bookkeeping)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)


async def _relay(client_ws: WebSocket, backend_ws_url: str) -> None:
    """Same relay shape as server_webrtc.hf_realtime_proxy (kept in sync).

    Preserves WebSocket subprotocol negotiation: forwards the client's offered
    subprotocols upstream and echoes the negotiated choice back, so a client that
    requires an acknowledged subprotocol (the OpenAI Agents Realtime SDK) does not
    abort the connection.
    """
    offered = [
        p.strip()
        for p in client_ws.headers.get("sec-websocket-protocol", "").split(",")
        if p.strip()
    ]
    try:
        upstream = await websockets.connect(
            backend_ws_url, max_size=None, subprotocols=offered or None
        )
    except Exception as e:
        await client_ws.accept()
        await client_ws.close(code=1013, reason=f"HF backend unavailable: {type(e).__name__}")
        return
    negotiated = getattr(upstream, "subprotocol", None) or (offered[0] if offered else None)
    await client_ws.accept(subprotocol=negotiated)

    async def to_upstream() -> None:
        while True:
            msg = await client_ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if (data := msg.get("bytes")) is not None:
                await upstream.send(data)
            elif (text := msg.get("text")) is not None:
                await upstream.send(text)

    async def to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await client_ws.send_bytes(message)
            else:
                await client_ws.send_text(message)

    to_up = asyncio.create_task(to_upstream())
    to_cl = asyncio.create_task(to_client())
    try:
        done, pending = await asyncio.wait({to_up, to_cl}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in done:
            with contextlib.suppress(Exception):
                task.result()
    finally:
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await upstream.wait_closed()
        with contextlib.suppress(Exception):
            await client_ws.close(code=1000)


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
        # Single-slot bookkeeping (mimics the HF backend's pool size 1). These are
        # only touched on the server loop thread, so no locking is needed for the
        # read-after-join assertions.
        self.active = 0
        self.max_active = 0

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def echo(conn):
            # Enforce a single occupied slot: the whole point of the leak test is
            # that the proxy must free this slot promptly when the browser goes.
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                async for message in conn:
                    if isinstance(message, bytes):
                        await conn.send(b"echo:" + message)
                    else:
                        await conn.send("echo:" + message)
            finally:
                self.active -= 1

        def select_subprotocol(conn, subprotocols):
            # Mimic the HF backend: echo "realtime" when offered, else none.
            return "realtime" if "realtime" in subprotocols else None

        async def boot():
            self._server = await websockets.serve(
                echo, "127.0.0.1", 0, select_subprotocol=select_subprotocol
            )
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


def test_offered_subprotocol_is_echoed_back() -> None:
    # ROOT-CAUSE REGRESSION: the OpenAI Realtime SDK offers a WebSocket
    # subprotocol ("realtime") and aborts if the server does not echo one back.
    # The proxy must forward the offer upstream and echo the negotiated choice —
    # previously it accepted with no subprotocol, so the SDK bailed and the UI
    # showed "[object Event]".
    with _EchoServer() as echo:
        client = TestClient(_app(echo.url))
        with client.websocket_connect("/hf-realtime", subprotocols=["realtime"]) as ws:
            # Starlette's TestClient exposes the accepted subprotocol on the
            # handshake response headers.
            assert ws.accepted_subprotocol == "realtime"
            ws.send_text("ping")
            assert ws.receive_text() == "echo:ping"


def test_no_subprotocol_client_still_works() -> None:
    # A plain client that offers no subprotocol must still connect (backend echoes
    # none), so the fix does not regress non-SDK callers.
    with _EchoServer() as echo:
        client = TestClient(_app(echo.url))
        with client.websocket_connect("/hf-realtime") as ws:
            assert ws.accepted_subprotocol in (None, "")
            ws.send_text("ping")
            assert ws.receive_text() == "echo:ping"


def test_client_close_frees_the_upstream_slot_immediately() -> None:
    # LIFECYCLE LEAK REGRESSION: the HF backend has ONE pipeline slot. When the
    # browser closes, the proxy must promptly close the upstream so the slot frees
    # and the NEXT connection succeeds. A leaked pump would hold the slot and wedge
    # the demo ("all pipeline slots in use").
    with _EchoServer() as echo:
        client = TestClient(_app(echo.url))
        with client.websocket_connect("/hf-realtime", subprotocols=["realtime"]) as ws:
            ws.send_text("first")
            assert ws.receive_text() == "echo:first"
        # After the client context exits (close), the upstream slot must free.
        _wait_until(lambda: echo.active == 0, timeout=3)
        assert echo.active == 0, "upstream slot leaked after client close"
        # And a second connection must immediately succeed on the freed slot.
        with client.websocket_connect("/hf-realtime", subprotocols=["realtime"]) as ws2:
            ws2.send_text("second")
            assert ws2.receive_text() == "echo:second"


def test_ten_switch_cycles_never_leak_and_max_active_is_one() -> None:
    # Simulate the tab-switch lifecycle: 10 connect→use→close cycles. The hard,
    # portal-safe invariant is that NO cycle ever overlaps another (max_active==1):
    # if a prior slot leaked, the echo server's single handler would still be live
    # when the next cycle connects and max_active would climb to 2. Each cycle also
    # completing a round-trip proves the freed slot was actually reusable.
    with _EchoServer() as echo:
        client = TestClient(_app(echo.url))
        for i in range(10):
            with client.websocket_connect("/hf-realtime", subprotocols=["realtime"]) as ws:
                ws.send_text(f"cycle-{i}")
                assert ws.receive_text() == f"echo:cycle-{i}"
            # Model the real lifecycle controller, which AWAITS the outgoing
            # session's teardown before activating the next tab. Here that means
            # waiting for the freed slot before the next connect — proving the slot
            # is reusable and never overlaps. (Without this await, a rapid back-to-
            # back reconnect can momentarily race the previous upstream's async
            # close; the client contract is to await teardown first.)
            _wait_until(lambda: echo.active == 0, timeout=3)
            assert echo.active == 0, f"slot not freed before cycle {i + 1}"
        assert echo.max_active == 1, f"more than one live session observed: {echo.max_active}"


def test_backend_down_closes_client_cleanly() -> None:
    # Point at a port with nothing listening: the proxy must close, not hang.
    app = _app("ws://127.0.0.1:1")  # port 1 = guaranteed refused
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/hf-realtime") as ws:
            ws.receive_text()


if __name__ == "__main__":
    test_text_and_binary_round_trip()
    test_offered_subprotocol_is_echoed_back()
    test_no_subprotocol_client_still_works()
    test_backend_down_closes_client_cleanly()
    print("proxy relay OK")
