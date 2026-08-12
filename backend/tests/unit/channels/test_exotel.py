"""Unit tests for Exotel telephony adapter."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fonely.api.channels.exotel import _read_opening_frames, router
from fonely.repositories.channel_identities import (
    PROVIDER_EXOTEL,
    ChannelIdentityRepository,
)
from fonely.services.audio_admission import (
    AdmissionRefusal,
    AdmissionResult,
    AudioSession,
)
from fonely.services.audio_stream import AudioStreamHandoff, StreamStartRefusal


class _ScriptedSocket:
    """A websocket that yields a fixed script of frames, then disconnects.

    Used where the refusal *reason* is the thing under test. Through
    TestClient only the close is observable, and every refusal closes — so a
    socket-level assertion cannot tell "we refused this audio" apart from "the
    frame budget ran out", which is precisely the distinction that matters
    here. Driving the reader directly makes the reason assertable.
    """

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)

    async def receive(self) -> dict[str, object]:
        if not self._frames:
            return {"type": "websocket.disconnect"}
        return {"type": "websocket.receive", "text": self._frames.pop(0)}


_SECRET = "exotel-test-secret"
_CALL_SID = "call-123"
_STREAM_SID = "stream-abc"


def _start_frame(
    *,
    call_sid: str | None = _CALL_SID,
    stream_sid: str | None = _STREAM_SID,
    encoding: str | None = "audio/x-l16",
    sample_rate: object = 8000,
    channels: object = 1,
) -> str:
    """A well-formed Exotel start event, with each field overridable.

    Every negative test below is this frame with exactly one thing wrong, so a
    refusal can only be attributed to the field that test changed.
    """
    media_format: dict[str, object] = {}
    if encoding is not None:
        media_format["encoding"] = encoding
    if sample_rate is not None:
        media_format["sample_rate"] = sample_rate
    if channels is not None:
        media_format["channels"] = channels

    start: dict[str, object] = {"media_format": media_format}
    if call_sid is not None:
        start["call_sid"] = call_sid
    if stream_sid is not None:
        start["stream_sid"] = stream_sid
    return json.dumps({"event": "start", "start": start})


@pytest.fixture(autouse=True)
def exotel_settings() -> Iterator[MagicMock]:
    """Give every test a configured secret.

    Autouse because the secret is now load-bearing for both entry points --
    without it every request is a 503, and a test that forgot to set it would
    pass its status assertion for entirely the wrong reason. Tests that need
    it absent set it to "" on the yielded mock.
    """
    with patch("fonely.api.channels.exotel.settings") as mock_settings:
        mock_settings.exotel_webhook_secret = _SECRET
        yield mock_settings


@pytest.fixture(autouse=True)
def channel_numbers() -> Iterator[dict[str, int]]:
    """Stand in for the business_channel_identities lookup.

    Autouse for the same reason as the secret: number resolution is now a
    database read, and a test that left it unstubbed would 404 for a reason
    unrelated to what it is asserting. Tests that want an unknown number
    mutate the yielded dict.
    """
    numbers = {"08012345678": 1}

    async def _resolve(
        self: ChannelIdentityRepository, provider: str, external_identifier: str
    ) -> int | None:
        assert provider == PROVIDER_EXOTEL
        return numbers.get(external_identifier)

    with patch.object(ChannelIdentityRepository, "resolve_business_id", _resolve):
        yield numbers


_ADMITTED_SESSION = AudioSession(
    business_id=1,
    call_id=42,
    caller_phone="+919876543210",
    clinic_name="Test Dental Clinic",
    timezone="Asia/Kolkata",
    provider=PROVIDER_EXOTEL,
    provider_call_sid=_CALL_SID,
)


@pytest.fixture(autouse=True)
def admission() -> Iterator[dict[str, AdmissionResult]]:
    """Stub the admission seam so these tests cover handler branching only.

    Whether a CallSid resolves to a tenant is decided by real SQL against real
    constraints, and is proven in the PostgreSQL integration tests. What is
    unit-testable here is what the handler does with each answer: admit, or
    close 1008 without ever handing over the socket.

    The stub keeps the two invariants the handler depends on -- an empty
    identifier never admits, and an unknown one never admits -- so a test
    cannot pass by accident on a socket that failed to identify itself.
    """
    outcomes = {_CALL_SID: AdmissionResult(session=_ADMITTED_SESSION, refusal=None)}

    async def _stub(
        db_session: object, *, provider: str, provider_call_sid: str
    ) -> AdmissionResult:
        assert provider == PROVIDER_EXOTEL
        if not provider_call_sid:
            return AdmissionResult(session=None, refusal=AdmissionRefusal.NO_CALL_IDENTIFIER)
        return outcomes.get(
            provider_call_sid,
            AdmissionResult(session=None, refusal=AdmissionRefusal.UNOBSERVED_CALL),
        )

    with patch("fonely.api.channels.exotel.admit_audio_stream", _stub):
        yield outcomes


def _create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 42
    mock_result.scalar_one_or_none.return_value = 42
    mock_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.session_factory = mock_factory
    app.state._mock_session = mock_session
    return app


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_SECRET}"}


def _basic(username: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


class TestCallStatusWebhook:
    def test_ringing_returns_200(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 200
        assert response.text == "ok"
        app.state._mock_session.execute.assert_awaited()
        app.state._mock_session.commit.assert_awaited()

    def test_answered_returns_200(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "answered",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 200

    def test_completed_updates_call(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "completed",
                "To": "08012345678",
                "From": "+919876543210",
                "Duration": "120",
            },
            headers=_auth(),
        )
        assert response.status_code == 200
        app.state._mock_session.execute.assert_awaited()
        app.state._mock_session.commit.assert_awaited()

    def test_missing_call_sid_returns_400(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={"Status": "ringing", "To": "08012345678"},
            headers=_auth(),
        )
        assert response.status_code == 400

    def test_unknown_number_returns_404(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "09999999999",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 404

    def test_failed_status_returns_200(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "failed",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 200


class TestCallStatusBodyEncoding:
    """Exotel posts form fields, not JSON.

    The handler only ever called request.json(), so a genuine callback would
    have raised before reaching any logic. Every pre-existing test posted
    JSON, which is why nothing caught it.
    """

    def test_form_encoded_callback_is_accepted(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            data={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 200
        app.state._mock_session.commit.assert_awaited()

    def test_form_encoded_completed_updates_call(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            data={
                "CallSid": "call-123",
                "Status": "completed",
                "To": "08012345678",
                "From": "+919876543210",
                "Duration": "120",
            },
            headers=_auth(),
        )
        assert response.status_code == 200

    def test_unparseable_body_is_a_400_not_a_500(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content=b"\x00\x01not-a-body",
            headers={"Content-Type": "application/json", **_auth()},
        )
        assert response.status_code == 400


class TestCallStatusAuthentication:
    """The route writes `calls` rows for a tenant, so it must not be open.

    Every case asserts the database was never touched as well as the status
    code -- a 401 returned after the insert would be worthless.
    """

    def _post(self, app: FastAPI, **kwargs: object) -> object:
        client = TestClient(app)
        return client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            **kwargs,  # type: ignore[arg-type]
        )

    def test_no_credential_is_rejected(self) -> None:
        app = _create_app()
        response = self._post(app)
        assert response.status_code == 401  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_wrong_bearer_is_rejected(self) -> None:
        app = _create_app()
        response = self._post(app, headers={"Authorization": "Bearer not-the-secret"})
        assert response.status_code == 401  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_basic_password_is_accepted(self) -> None:
        """Credentials embedded in the callback URL — Exotel's documented way."""
        app = _create_app()
        response = self._post(app, headers=_basic("exotel", _SECRET))
        assert response.status_code == 200  # type: ignore[attr-defined]

    def test_basic_whole_pair_is_accepted(self) -> None:
        app = _create_app()
        encoded = base64.b64encode(_SECRET.encode()).decode()
        response = self._post(app, headers={"Authorization": f"Basic {encoded}"})
        assert response.status_code == 200  # type: ignore[attr-defined]

    def test_basic_wrong_password_is_rejected(self) -> None:
        app = _create_app()
        response = self._post(app, headers=_basic("exotel", "wrong"))
        assert response.status_code == 401  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_query_token_is_accepted(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            f"/webhooks/exotel/call-status?token={_SECRET}",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
        )
        assert response.status_code == 200

    def test_wrong_query_token_is_rejected(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status?token=nope",
            json={"CallSid": "call-123", "Status": "ringing", "To": "08012345678"},
        )
        assert response.status_code == 401
        app.state._mock_session.execute.assert_not_awaited()

    @pytest.mark.parametrize(
        "header",
        [
            "Basic not-valid-base64!!",
            "Basic " + base64.b64encode(b"\xff\xfe").decode(),
            "Bearer",
            "Basic",
            "Digest something",
            "",
        ],
    )
    def test_malformed_credentials_are_rejected_not_crashed(self, header: str) -> None:
        app = _create_app()
        response = self._post(app, headers={"Authorization": header})
        assert response.status_code == 401  # type: ignore[attr-defined]

    def test_non_ascii_credential_is_rejected_not_crashed(self) -> None:
        """compare_digest raises on non-ASCII str; a 500 here would leak shape."""
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status?token=கா",
            json={"CallSid": "call-123", "Status": "ringing", "To": "08012345678"},
        )
        assert response.status_code == 401

    def test_unconfigured_secret_returns_503_and_rejects(self, exotel_settings: MagicMock) -> None:
        exotel_settings.exotel_webhook_secret = ""
        app = _create_app()
        response = self._post(app, headers=_auth())
        assert response.status_code == 503  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_empty_secret_does_not_admit_empty_credential(self, exotel_settings: MagicMock) -> None:
        """An unset secret must never make an empty credential valid."""
        exotel_settings.exotel_webhook_secret = ""
        app = _create_app()
        response = self._post(app, headers={"Authorization": "Bearer "})
        assert response.status_code == 503  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()


class TestAudioStreamWebSocket:
    def test_connects_with_valid_credential(self) -> None:
        app = _create_app()
        app.state.voice_audio_runtime = _RecordingRuntime()
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?CallSid={_CALL_SID}", headers=_auth()
        ) as ws:
            ws.send_text(_start_frame())
            ws.close()

    def test_connects_with_query_token(self) -> None:
        """The media-stream applet takes a URL, so this is often the only carrier."""
        app = _create_app()
        app.state.voice_audio_runtime = _RecordingRuntime()
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(_start_frame())
            ws.close()

    def test_unauthenticated_connection_is_refused(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream") as ws,
        ):
            ws.send_bytes(b"\x00" * 320)

    def test_wrong_credential_connection_is_refused(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream?token=wrong") as ws,
        ):
            ws.send_bytes(b"\x00" * 320)

    def test_refused_before_handshake_completes(self) -> None:
        """Rejection must precede accept().

        Accepting and then closing would confirm the endpoint to an
        unauthenticated caller and hand them a live socket for the duration
        of the close. Starlette surfaces a pre-accept close as a rejection at
        connect time, which is what this asserts.
        """
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream"),
        ):
            pass

    def test_unconfigured_secret_refuses_connection(self, exotel_settings: MagicMock) -> None:
        exotel_settings.exotel_webhook_secret = ""
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream"),
        ):
            pass


class _RecordingRuntime:
    """Stands in for the voice runtime and records what it was handed."""

    def __init__(self) -> None:
        self.sessions: list[AudioSession] = []
        self.handoffs: list[AudioStreamHandoff] = []

    async def handle_audio_session(
        self, websocket: object, session: AudioSession, handoff: AudioStreamHandoff
    ) -> None:
        self.sessions.append(session)
        self.handoffs.append(handoff)


class TestAudioStreamAdmission:
    """The secret proves provenance; admission decides which clinic, if any."""

    def test_unobserved_call_sid_is_refused_before_accept(self) -> None:
        """A forged or guessed CallSid never reaches a tenant.

        Refusal happens before accept(), so a caller holding a leaked applet
        URL never gets a connected socket out of a call we never saw ring.
        """
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid=forged-sid"
            ),
        ):
            pass
        assert runtime.sessions == []

    def test_query_call_sid_admits_and_hands_typed_session_to_runtime(self) -> None:
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(_start_frame())

        assert len(runtime.sessions) == 1
        session = runtime.sessions[0]
        # Every one of these was resolved server-side. None of it was read off
        # the socket, which is the whole point of the seam.
        assert session.business_id == 1
        assert session.call_id == 42
        assert session.clinic_name == "Test Dental Clinic"
        assert session.timezone == "Asia/Kolkata"
        assert session.provider_call_sid == _CALL_SID

    def test_opening_frame_call_sid_admits(self) -> None:
        """Fallback path: the console could not template the id into the URL."""
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(f"/webhooks/exotel/audio-stream?token={_SECRET}") as ws:
            ws.send_text(_start_frame())

        assert len(runtime.sessions) == 1
        assert runtime.sessions[0].business_id == 1

    def test_unidentifiable_opening_frames_are_refused(self) -> None:
        """A socket that authenticates but never identifies itself is closed.

        It must not be able to hold a worker open either, which is what the
        frame budget bounds -- this exhausts it rather than waiting out the
        timeout.
        """
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(f"/webhooks/exotel/audio-stream?token={_SECRET}") as ws:
            for _ in range(6):
                ws.send_bytes(b"\x00" * 320)
            message = ws.receive()

        # 1008 is policy violation: we are refusing the caller, not reporting
        # a fault of our own. The distinction matters when reading provider
        # logs after a failed call.
        assert message["type"] == "websocket.close"
        assert message["code"] == 1008
        assert runtime.sessions == []

    def test_ended_call_does_not_readmit(self, admission: dict[str, AdmissionResult]) -> None:
        """A completed conversation is not reopenable by replaying its id."""
        admission["ended-call"] = AdmissionResult(
            session=None, refusal=AdmissionRefusal.CALL_ALREADY_ENDED
        )
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid=ended-call"
            ),
        ):
            pass
        assert runtime.sessions == []


class TestChannelIdentityResolutionOnWebhook:
    def test_resolution_is_scoped_to_the_exotel_provider(self) -> None:
        """The same digits under another provider must not resolve here."""
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": _CALL_SID,
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        # The stub asserts the provider it was called with; a wrong provider
        # would surface as an error rather than a silent cross-provider hit.
        assert response.status_code == 200

    def test_disabled_identity_refuses_the_call(self, channel_numbers: dict[str, int]) -> None:
        """A decommissioned number stops writing into the clinic's records."""
        channel_numbers.clear()
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": _CALL_SID,
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 404
        app.state._mock_session.commit.assert_not_awaited()


class TestStartMetadataHandoff:
    """The start event is parsed and passed on, not consumed and discarded.

    The provider does not replay control frames. Whatever this adapter reads
    off the socket and drops is gone, and the serializer downstream cannot send
    a single frame back without the stream id, nor decode audio without the
    declared rate. A seam that admits the call correctly and then makes it
    unanswerable is not a working seam.
    """

    def test_runtime_receives_parsed_start_metadata(self) -> None:
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(_start_frame(sample_rate=16000))

        assert len(runtime.handoffs) == 1
        start = runtime.handoffs[0].start
        assert start.stream_sid == _STREAM_SID
        assert start.sample_rate == 16000
        assert start.encoding == "l16"
        assert start.channels == 1

    def test_raw_frames_are_retained_for_the_serializer(self) -> None:
        """A serializer that would rather parse the provider's own frame can.

        Both frames read before handoff are kept in arrival order, so the
        connected event is not silently lost either.
        """
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(_start_frame())

        raw = runtime.handoffs[0].raw_frames
        assert len(raw) == 2
        assert json.loads(raw[0])["event"] == "connected"
        assert json.loads(raw[1])["event"] == "start"

    @pytest.mark.parametrize("rate", [8000, 16000, 24000])
    def test_declared_rate_is_carried_through_not_assumed(self, rate: int) -> None:
        """No hardcoded demo rate. What the provider declared is what is passed."""
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(_start_frame(sample_rate=rate))

        assert runtime.handoffs[0].start.sample_rate == rate

    def test_string_sample_rate_is_accepted(self) -> None:
        """Consoles send these as strings; refusing a valid call over a quote is a defect."""
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(_start_frame(sample_rate="16000"))

        assert runtime.handoffs[0].start.sample_rate == 16000


class TestStartValidationFailsClosed:
    """Each case is the good frame with exactly one thing wrong."""

    def _refused(self, frame: str) -> _RecordingRuntime:
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(frame)
            message = ws.receive()
        assert message["type"] == "websocket.close"
        return runtime

    def test_unsupported_sample_rate_is_refused(self) -> None:
        """Accepting 44100 and resampling on a guess produces transcribable noise."""
        assert self._refused(_start_frame(sample_rate=44100)).sessions == []

    def test_unsupported_encoding_is_refused(self) -> None:
        assert self._refused(_start_frame(encoding="opus")).sessions == []

    def test_stereo_is_refused(self) -> None:
        """Two interleaved legs would be transcribed as one speaker."""
        assert self._refused(_start_frame(channels=2)).sessions == []

    def test_missing_stream_sid_is_refused(self) -> None:
        """Without it no frame can be sent back, so the call is unanswerable."""
        assert self._refused(_start_frame(stream_sid=None)).sessions == []

    def test_missing_media_format_is_refused(self) -> None:
        frame = json.dumps({"event": "start", "start": {"call_sid": _CALL_SID}})
        assert self._refused(frame).sessions == []

    def test_malformed_start_is_refused_not_crashed(self) -> None:
        assert self._refused('{"event": "start", not json').sessions == []

    @pytest.mark.asyncio
    async def test_media_before_start_is_refused_by_reason(self) -> None:
        """Decoding audio before the format is declared means guessing the rate.

        The media frame is followed by a perfectly valid start, so without the
        guard the stream is *admitted* on that later start with the early audio
        silently skipped. Asserting only "refused" would pass for the wrong
        reason too, since a budget-exhausted socket also refuses. The reason is
        the discriminator, so the reason is what this asserts.
        """
        socket = _ScriptedSocket(
            [
                json.dumps({"event": "media", "media": {"payload": "AAAA"}}),
                _start_frame(),
            ]
        )
        result = await _read_opening_frames(socket)  # type: ignore[arg-type]

        assert not result.ok
        assert result.refusal is StreamStartRefusal.MEDIA_BEFORE_START

    @pytest.mark.asyncio
    async def test_a_valid_start_alone_is_not_refused(self) -> None:
        """The negative control for the test above.

        Without this, a reader that refused every stream would satisfy the
        media-before-start assertion by accident.
        """
        result = await _read_opening_frames(
            _ScriptedSocket([_start_frame()])  # type: ignore[arg-type]
        )

        assert result.ok
        assert result.refusal is None
        assert result.handoff is not None
        assert result.handoff.start.stream_sid == _STREAM_SID

    def test_media_before_start_closes_the_socket(self) -> None:
        """The refusal above is actually wired to the live endpoint."""
        app = _create_app()
        runtime = _RecordingRuntime()
        app.state.voice_audio_runtime = runtime
        client = TestClient(app)
        with client.websocket_connect(
            f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
        ) as ws:
            ws.send_text(json.dumps({"event": "media", "media": {"payload": "AAAA"}}))
            message = ws.receive()

        assert message["type"] == "websocket.close"
        assert runtime.sessions == []

    def test_channels_true_does_not_read_as_mono(self) -> None:
        """bool is an int in Python; channels=true must not pass as 1."""
        assert self._refused(_start_frame(channels=True)).sessions == []


class TestNoRuntimeIsRefusedNotDrained:
    def test_unmounted_runtime_refuses_before_handshake(self) -> None:
        """Draining a patient's call is indistinguishable from serving it.

        The old behaviour accepted the socket and read frames forever: the line
        stayed open, nothing was said, nothing was booked, and the provider log
        looked like a normal call. Absence must not read as success.
        """
        app = _create_app()
        assert not hasattr(app.state, "voice_audio_runtime")
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/webhooks/exotel/audio-stream?token={_SECRET}&CallSid={_CALL_SID}"
            ),
        ):
            pass
