"""Mounted Exotel WebSocket acceptance probe.

The injected runtime fixture proves adapter handoff into Pipecat and
provider JSON serialization. It does not prove Dev4's combined-tree
runtime factory; that remains an integration gate.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.frames.frames import OutputAudioRawFrame, StartFrame
from starlette.websockets import WebSocketDisconnect

from fonely.api.channels.exotel_admission import StreamAdmissionController
from fonely.api.channels.exotel_stream import (
    ExotelStreamSession,
    router,
)
from fonely.domain.calls.correlation import (
    CorrelationOutcome,
    InMemoryCorrelationStore,
)
from fonely.services.exotel_config import ExotelNumberMapping
from tests.fixtures.exotel_callbacks.simulator import ExotelSimulator, SimulatorConfig

_SECRET = "test-gateway-secret-value-at-least-32-chars"


def _app_with_prerequisites(runtime_factory):
    app = FastAPI()
    app.include_router(router)
    app.state.exotel_gateway_secret = _SECRET
    app.state.exotel_mapping = ExotelNumberMapping({"08012345678": 1})
    app.state.exotel_correlation = InMemoryCorrelationStore()
    app.state.exotel_admission = StreamAdmissionController(1, 2)
    app.state.exotel_runtime_factory = runtime_factory
    app.state.exotel_account_id = "AC_simulator"
    app.state.exotel_environment = "sandbox"
    app.state.exotel_expected_sample_rate = 16000
    app.state.exotel_session_timeout_seconds = 30
    return app


class RuntimeFixture:
    """Transport-aware fixture, not Dev4's production runtime."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[ExotelStreamSession] = []
        self.transport = None
        self.fail = fail

    async def __call__(self, transport, session: ExotelStreamSession) -> None:
        self.transport = transport
        self.calls.append(session)
        serializer = transport._params.serializer
        assert serializer is not None
        await serializer.setup(
            StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=24000)
        )
        provider_json = None
        for _ in range(12):
            provider_json = await serializer.serialize(
                OutputAudioRawFrame(
                    audio=b"\x00" * 4800,
                    sample_rate=24000,
                    num_channels=1,
                )
            )
            if provider_json is not None:
                break
        assert isinstance(provider_json, str)
        await transport._client.send(provider_json)
        if self.fail:
            raise RuntimeError("runtime fixture failure")


def _start_message(rate: int = 16000) -> dict:
    sim = ExotelSimulator(
        SimulatorConfig(
            call_sid="CA" + "a" * 30,
            stream_sid="MZ" + "b" * 30,
            account_sid="AC_simulator",
            from_number="+919000000001",
            to_number="08012345678",
            sample_rate=rate,
        )
    )
    return json.loads(sim.start_msg())


class TestMountedStream:
    def test_route_absent_without_mount(self) -> None:
        app = FastAPI()
        paths = {route.path for route in app.routes}
        assert "/webhooks/exotel/media" not in paths

    def test_direct_request_rejected_before_runtime(self) -> None:
        """No gateway secret → WebSocket closed with 4401, no admission."""
        runtime = RuntimeFixture()
        app = _app_with_prerequisites(runtime)
        with (
            TestClient(app) as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/webhooks/exotel/media"),
        ):
            pass
        assert exc_info.value.code == 4401
        assert runtime.calls == []
        assert app.state.exotel_admission.counts() == (0, 0)
        assert app.state.exotel_admission.active() == 0

    def test_authenticated_stream_binds_and_emits_provider_json(self) -> None:
        runtime = RuntimeFixture()
        app = _app_with_prerequisites(runtime)
        with TestClient(app) as client, client.websocket_connect(
            "/webhooks/exotel/media",
            headers={"X-Exotel-Webhook-Secret": _SECRET},
        ) as ws:
            ws.send_json(_start_message())
            provider_message = ws.receive_json()

        assert provider_message["event"] == "media"
        assert provider_message["streamSid"] == "MZ" + "b" * 30
        assert len(runtime.calls) == 1
        session = runtime.calls[0]
        assert session.business_id == 1
        assert session.metadata.call_sid == "CA" + "a" * 30
        assert session.metadata.sample_rate == 16000
        assert session.provisioning_drift is False
        assert app.state.exotel_admission.counts() == (1, 1)
        assert app.state.exotel_admission.active() == 0

        result = _correlate(app, session)
        assert result.outcome is CorrelationOutcome.MATCHED

    def test_runtime_error_still_releases_exactly_once(self) -> None:
        """Runtime failure → admission slot released in finally."""
        runtime = RuntimeFixture(fail=True)
        app = _app_with_prerequisites(runtime)
        with TestClient(app) as client:
            try:
                with client.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_json(_start_message())
            except (WebSocketDisconnect, RuntimeError):
                pass
        assert len(runtime.calls) == 1
        assert app.state.exotel_admission.counts() == (1, 1)
        assert app.state.exotel_admission.active() == 0

    def test_account_mismatch_never_admits(self) -> None:
        """Wrong account_sid → closed with protocol error, no admission."""
        runtime = RuntimeFixture()
        app = _app_with_prerequisites(runtime)
        message = _start_message()
        message["start"]["account_sid"] = "AC_wrong"
        with (
            TestClient(app) as client,
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(message)
            ws.receive_text()
        assert exc_info.value.code == 4400
        assert runtime.calls == []
        assert app.state.exotel_admission.counts() == (0, 0)



def _correlate(app, session):
    import asyncio

    return asyncio.run(
        app.state.exotel_correlation.correlate(
            provider="exotel",
            provider_account_id=session.metadata.account_sid,
            provider_call_id=session.metadata.call_sid,
            called_number=session.metadata.to_number,
            business_id=session.business_id,
            direction=session.metadata.direction,
        )
    )
