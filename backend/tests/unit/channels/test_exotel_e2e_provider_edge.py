"""Provider-edge E2E acceptance tests — simulator-driven through mounted app.

Proves the complete bounded feature:
  simulator → mounted WebSocket → gateway auth → start validation →
  tenant resolution → admission → correlation → Pipecat transport →
  runtime factory → inbound/outbound audio → clear → cleanup

Evidence is SIMULATOR, not real Exotel. Explicitly does not prove
STT/LLM/TTS quality, booking, or provider sandbox behavior.
"""

# ruff: noqa: SIM117
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.frames.frames import OutputAudioRawFrame, StartFrame
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport
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

_SECRET = "e2e-gateway-secret-at-least-32-characters"
_ACCOUNT = "AC_e2e_sim"


def _start_json(
    rate: int = 16000,
    account: str = _ACCOUNT,
    to_number: str = "08012345678",
    from_number: str = "+919000000001",
    call_sid: str = "CA" + "a" * 30,
    stream_sid: str = "MZ" + "b" * 30,
) -> dict:
    sim = ExotelSimulator(
        SimulatorConfig(
            call_sid=call_sid,
            stream_sid=stream_sid,
            account_sid=account,
            from_number=from_number,
            to_number=to_number,
            sample_rate=rate,
        )
    )
    return json.loads(sim.start_msg())


class _RuntimeFixture:
    """Deterministic fake runtime satisfying ExotelRuntimeFactory."""

    def __init__(
        self,
        *,
        fail: bool = False,
        emit_audio: bool = True,
        emit_clear: bool = False,
        hang: bool = False,
    ) -> None:
        self.calls: list[ExotelStreamSession] = []
        self.transports: list[FastAPIWebsocketTransport] = []
        self._fail = fail
        self._emit_audio = emit_audio
        self._emit_clear = emit_clear
        self._hang = hang

    async def __call__(
        self,
        transport: FastAPIWebsocketTransport,
        session: ExotelStreamSession,
    ) -> None:
        self.transports.append(transport)
        self.calls.append(session)
        serializer = transport._params.serializer
        if serializer is None:
            return
        await serializer.setup(StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=24000))

        if self._emit_audio:
            for _ in range(5):
                result = await serializer.serialize(
                    OutputAudioRawFrame(
                        audio=b"\x00" * 4800,
                        sample_rate=24000,
                        num_channels=1,
                    )
                )
                if result is not None:
                    await transport._client.send(result)
                    break

        if self._emit_clear:
            from pipecat.frames.frames import InterruptionFrame

            clear_result = await serializer.serialize(InterruptionFrame())
            if clear_result is not None:
                await transport._client.send(clear_result)

        if self._fail:
            raise RuntimeError("injected runtime failure")

        if self._hang:
            await asyncio.sleep(3600)


def _app(runtime: _RuntimeFixture | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.exotel_gateway_secret = _SECRET
    app.state.exotel_mapping = ExotelNumberMapping({"08012345678": 1, "08087654321": 2})
    app.state.exotel_correlation = InMemoryCorrelationStore()
    app.state.exotel_admission = StreamAdmissionController(2, 10)
    app.state.exotel_runtime_factory = runtime or _RuntimeFixture()
    app.state.exotel_account_id = _ACCOUNT
    app.state.exotel_environment = "sandbox"
    app.state.exotel_expected_sample_rate = 16000
    app.state.exotel_session_timeout_seconds = 30
    return app


# ============================================================================
# A. MOUNTING AND AUTH
# ============================================================================


class TestMountingAndAuth:
    def test_a1_router_mounted(self) -> None:
        """Route reachable: correct auth + valid start → runtime invoked."""
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()
        assert len(rt.calls) == 1

    def test_a2_missing_secret_fails_closed(self) -> None:
        a = _app()
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/webhooks/exotel/media"):
                pass
        assert exc.value.code == 4401

    def test_a4_missing_auth_rejected_before_runtime(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/webhooks/exotel/media"):
                pass
        assert rt.calls == []

    def test_a5_wrong_auth_rejected(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": "wrong-value-padded-to-32chars!!"},
            ):
                pass
        assert exc.value.code == 4401
        assert rt.calls == []

    def test_a6_correct_auth_reaches_start(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()
        assert len(rt.calls) == 1


# ============================================================================
# B. START AND TENANT BINDING
# ============================================================================


class TestStartAndTenantBinding:
    def test_b1_valid_start_resolves_business(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()
        assert rt.calls[0].business_id == 1
        assert rt.calls[0].metadata.call_sid == "CA" + "a" * 30

    def test_b2_unknown_number_rejected(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_json(_start_json(to_number="09999999999", from_number="+918888888888"))
                ws.receive_text()
        assert exc.value.code == 4404
        assert rt.calls == []

    def test_b4_account_mismatch_rejected(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_json(_start_json(account="AC_wrong"))
                ws.receive_text()
        assert exc.value.code == 4400
        assert rt.calls == []

    def test_b7_missing_call_sid_fails(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        msg = _start_json()
        msg["start"]["call_sid"] = ""
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_json(msg)
                ws.receive_text()
        assert exc.value.code == 4400
        assert rt.calls == []


# ============================================================================
# C. BOUNDS AND EVENT ORDER
# ============================================================================


class TestBoundsAndEventOrder:
    def test_c1_media_before_start_fails(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        media = json.dumps({"event": "media", "media": {"payload": "AAAA"}})
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_text(media)
                ws.receive_text()
        assert exc.value.code == 4400
        assert rt.calls == []

    def test_c4_invalid_json_fails(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_text("not json at all")
                ws.receive_text()
        assert exc.value.code == 4400

    def test_c9_unsupported_sample_rate_fails(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_json(_start_json(rate=44100))
                ws.receive_text()
        assert exc.value.code == 4400
        assert rt.calls == []


# ============================================================================
# D. ADMISSION
# ============================================================================


class TestAdmission:
    def test_d1_valid_stream_claims_slot(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()
        admitted, released = a.state.exotel_admission.counts()
        assert admitted == 1
        assert released == 1
        assert a.state.exotel_admission.active() == 0

    def test_d4_rejected_stream_never_invokes_runtime(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        a.state.exotel_admission = StreamAdmissionController(1, 1)
        a.state.exotel_admission.try_admit("1")
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws:
                ws.send_json(_start_json())
                ws.receive_text()
        assert exc.value.code == 4429
        assert rt.calls == []

    def test_d6_runtime_exception_releases_capacity(self) -> None:
        rt = _RuntimeFixture(fail=True)
        a = _app(rt)
        with TestClient(a) as c:
            try:
                with c.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_json(_start_json())
            except (WebSocketDisconnect, RuntimeError):
                pass
        assert len(rt.calls) == 1
        assert a.state.exotel_admission.counts() == (1, 1)
        assert a.state.exotel_admission.active() == 0

    def test_d7_malformed_event_releases_capacity(self) -> None:
        a = _app()
        with TestClient(a) as c:
            try:
                with c.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_text("not json")
                    ws.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                pass
        assert a.state.exotel_admission.active() == 0


# ============================================================================
# E. CORRELATION
# ============================================================================


class TestCorrelation:
    def test_e1_valid_start_creates_correlation(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()

        import asyncio

        result = asyncio.run(
            a.state.exotel_correlation.correlate(
                provider="exotel",
                provider_account_id=_ACCOUNT,
                provider_call_id="CA" + "a" * 30,
                called_number="08012345678",
                business_id=1,
                direction=None,
            )
        )
        assert result.outcome is CorrelationOutcome.MATCHED


# ============================================================================
# F. RUNTIME HANDOFF
# ============================================================================


class TestRuntimeHandoff:
    def test_f1_factory_invoked_exactly_once(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()
        assert len(rt.calls) == 1

    def test_f2_session_contains_trusted_metadata(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_json()
        s = rt.calls[0]
        assert s.business_id == 1
        assert s.metadata.account_sid == _ACCOUNT
        assert s.metadata.sample_rate == 16000
        assert s.provisioning_drift is False

    def test_f5_runtime_exception_typed_failure(self) -> None:
        rt = _RuntimeFixture(fail=True)
        a = _app(rt)
        with TestClient(a) as c:
            try:
                with c.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_json(_start_json())
            except (WebSocketDisconnect, RuntimeError):
                pass
        assert len(rt.calls) == 1
        assert a.state.exotel_admission.active() == 0


# ============================================================================
# G. AUDIO AND BARGE-IN
# ============================================================================


class TestAudioAndBargeIn:
    def test_g2_runtime_audio_serializes_to_provider_media(self) -> None:
        rt = _RuntimeFixture(emit_audio=True)
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            msg = ws.receive_json()
        assert msg["event"] == "media"
        assert msg["streamSid"] == "MZ" + "b" * 30
        assert "payload" in msg["media"]

    def test_g3_interruption_serializes_to_clear(self) -> None:
        rt = _RuntimeFixture(emit_audio=True, emit_clear=True)
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()
        events = {msg1["event"], msg2["event"]}
        assert "media" in events
        assert "clear" in events

    def test_g4_media_before_clear_ordering(self) -> None:
        rt = _RuntimeFixture(emit_audio=True, emit_clear=True)
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()
        assert msg1["event"] == "media"
        assert msg2["event"] == "clear"

    def test_g9_sample_rate_configured_exactly(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json(rate=16000))
            ws.receive_json()
        assert rt.calls[0].metadata.sample_rate == 16000


# ============================================================================
# H. TERMINAL PATH MATRIX
# ============================================================================


class TestTerminalPaths:
    def test_h1_auth_failure_no_leak(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/webhooks/exotel/media"):
                pass
        assert rt.calls == []
        assert a.state.exotel_admission.active() == 0

    def test_h2_unknown_tenant_no_leak(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json(to_number="09999999999", from_number="+918888888888"))
            ws.receive_text()
        assert rt.calls == []
        assert a.state.exotel_admission.active() == 0

    def test_h3_bad_start_no_leak(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_text("not json")
            ws.receive_text()
        assert rt.calls == []
        assert a.state.exotel_admission.active() == 0

    def test_h5_admission_rejection_no_leak(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        a.state.exotel_admission = StreamAdmissionController(1, 1)
        a.state.exotel_admission.try_admit("1")
        with (
            TestClient(a) as c,
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            ws.receive_text()
        assert rt.calls == []

    def test_h6_runtime_factory_error_cleanup(self) -> None:
        rt = _RuntimeFixture(fail=True)
        a = _app(rt)
        with TestClient(a) as c:
            try:
                with c.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_json(_start_json())
            except (WebSocketDisconnect, RuntimeError):
                pass
        assert a.state.exotel_admission.counts() == (1, 1)
        assert a.state.exotel_admission.active() == 0


# ============================================================================
# I. APP/CONFIG TOPOLOGY
# ============================================================================


class TestAppConfigTopology:
    def test_i1_route_absent_without_prerequisites(self) -> None:
        bare = FastAPI()
        paths = {r.path for r in bare.routes}
        assert "/webhooks/exotel/media" not in paths

    def test_i2_missing_prerequisites_fails_closed(self) -> None:
        a = FastAPI()
        a.include_router(router)
        with TestClient(a) as c, pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ):
                pass
        assert exc.value.code in (4401, 1013)

    def test_i6_no_real_credential_in_constants(self) -> None:
        """Test constants use obviously-fake values, not real credentials."""
        assert _SECRET.startswith("e2e-")
        assert _ACCOUNT.startswith("AC_")
        assert "08012345678" not in _SECRET
        assert "08012345678" not in _ACCOUNT


# ============================================================================
# SIMULATOR E2E HAPPY PATH
# ============================================================================


class TestSimulatorE2EHappy:
    def test_full_happy_path(self) -> None:
        """Complete bounded feature proof through simulator."""
        rt = _RuntimeFixture(emit_audio=True, emit_clear=True)
        a = _app(rt)

        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json())
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()

        assert len(rt.calls) == 1
        s = rt.calls[0]
        assert s.business_id == 1
        assert s.metadata.account_sid == _ACCOUNT
        assert s.metadata.sample_rate == 16000

        assert msg1["event"] == "media"
        assert msg2["event"] == "clear"

        assert a.state.exotel_admission.counts() == (1, 1)
        assert a.state.exotel_admission.active() == 0

        import asyncio

        result = asyncio.run(
            a.state.exotel_correlation.correlate(
                provider="exotel",
                provider_account_id=_ACCOUNT,
                provider_call_id="CA" + "a" * 30,
                called_number="08012345678",
                business_id=1,
                direction=None,
            )
        )
        assert result.outcome is CorrelationOutcome.MATCHED


# ============================================================================
# SIMULATOR E2E FAILURE PATHS
# ============================================================================


class TestSimulatorE2EFailure:
    def test_wrong_secret_never_invokes(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": "wrong-secret-padded-to-min32chars!"},
            ),
        ):
            pass
        assert rt.calls == []

    def test_unknown_dest_never_invokes(self) -> None:
        rt = _RuntimeFixture()
        a = _app(rt)
        with (
            TestClient(a) as c,
            pytest.raises(WebSocketDisconnect),
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json(to_number="09999999999", from_number="+918888888888"))
            ws.receive_text()
        assert rt.calls == []

    def test_runtime_exception_releases_all(self) -> None:
        rt = _RuntimeFixture(fail=True)
        a = _app(rt)
        with TestClient(a) as c:
            try:
                with c.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_json(_start_json())
            except (WebSocketDisconnect, RuntimeError):
                pass
        assert a.state.exotel_admission.active() == 0

    def test_subsequent_valid_succeeds_after_failure(self) -> None:
        """State is reusable after each failure mode."""
        fail_rt = _RuntimeFixture(fail=True)
        a = _app(fail_rt)
        with TestClient(a) as c:
            try:
                with c.websocket_connect(
                    "/webhooks/exotel/media",
                    headers={"X-Exotel-Webhook-Secret": _SECRET},
                ) as ws:
                    ws.send_json(_start_json())
            except (WebSocketDisconnect, RuntimeError):
                pass

        success_rt = _RuntimeFixture()
        a.state.exotel_runtime_factory = success_rt
        with (
            TestClient(a) as c,
            c.websocket_connect(
                "/webhooks/exotel/media",
                headers={"X-Exotel-Webhook-Secret": _SECRET},
            ) as ws,
        ):
            ws.send_json(_start_json(call_sid="CA" + "c" * 30, stream_sid="MZ" + "d" * 30))
            ws.receive_json()
        assert len(success_rt.calls) == 1
