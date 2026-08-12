"""Smoke test for the provider-free demo server."""

import sys

sys.path.insert(0, "backend/src")

import pytest
from httpx import ASGITransport, AsyncClient

from fonely.voice.demo_server import create_demo_app


@pytest.mark.asyncio
async def test_health():
    app = create_demo_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert d["mode"] == "demo"


@pytest.mark.asyncio
async def test_index_html():
    app = create_demo_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert "Fonely" in r.text
        assert "WebSocket" in r.text
