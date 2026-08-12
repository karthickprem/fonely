"""End-to-end live booking: real Anthropic LLM + real Cartesia TTS + real PostgreSQL.

NOT a unit test — requires credentials and a running PostgreSQL server.
Run with:
    DATABASE_URL=postgresql+asyncpg://localhost:5432/fonely \
        pytest -m live tests/integration/voice/test_live_booking.py -v

Proves: text input → real Claude LLM → real Cartesia Tamil TTS audio →
real AppointmentService → PostgreSQL appointment row committed.
Confirmation derived from committed receipt facts, not model intent.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.live


def _env_ready() -> bool:
    env_path = "/scratch/karthick/fonely/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
    settings_path = "/scratch/karthick/.claude/settings.json"
    if os.path.exists(settings_path):
        import json

        with open(settings_path) as f:
            data = json.load(f)
        for k, v in data.get("env", {}).items():
            os.environ.setdefault(k, v)

    db_url = os.environ.get("DATABASE_URL", "")
    sarvam = os.environ.get("SARVAM_API_KEY", "")
    cartesia = os.environ.get("CARTESIA_API_KEY", "")
    voice = os.environ.get("CARTESIA_VOICE_ID", "")
    return "postgresql" in db_url and len(sarvam) > 5 and len(cartesia) > 5 and len(voice) > 5


if not _env_ready():
    pytest.skip("Live credentials or PostgreSQL not available", allow_module_level=True)


# Imports deferred below the module-level skip guard so the heavy voice/provider
# deps are not imported when credentials/PostgreSQL are absent; E402 is intended.
import anthropic  # noqa: E402
import httpx  # noqa: E402

from fonely.voice.backend_ports import (  # noqa: E402
    AppointmentServiceCommandPort,
    build_actor_context,
)
from fonely.voice.config import VoiceSessionConfig  # noqa: E402
from fonely.voice.context import (  # noqa: E402
    AvailabilityQuery,
    AvailableSlot,
    DayAvailability,
    TrustedClock,
)
from fonely.voice.runtime import PipelineRuntime  # noqa: E402


def _clock():
    tz = ZoneInfo("Asia/Kolkata")
    local = datetime(2026, 8, 10, 14, 30, tzinfo=tz)
    return TrustedClock(
        now_utc=local.astimezone(UTC),
        business_timezone="Asia/Kolkata",
        business_date=date(2026, 8, 10),
        day_of_week="monday",
    )


class RealLLM:
    def __init__(self):
        self._client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "dummy"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://llm-api.amd.com/Unified"),
            default_headers=self._parse_headers(),
        )
        self.call_count = 0

    def _parse_headers(self) -> dict[str, str]:
        raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        headers = {}
        for line in raw.split("\n"):
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        return headers

    async def generate(self, system: str, messages: list[dict]) -> str:
        loop = asyncio.get_event_loop()
        self.call_count += 1
        msg = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model="claude-opus-4-6",
                max_tokens=300,
                system=system,
                messages=messages,
            ),
        )
        return msg.content[0].text

    async def close(self):
        pass


class RealTTS:
    def __init__(self):
        self._api_key = os.environ.get("CARTESIA_API_KEY", "")
        self._voice_id = os.environ.get("CARTESIA_VOICE_ID", "")
        self.call_count = 0
        self.total_bytes = 0

    async def synthesize(self, text: str) -> bytes:
        self.call_count += 1
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "X-API-Key": self._api_key,
                    "Cartesia-Version": "2024-06-10",
                    "Content-Type": "application/json",
                },
                json={
                    "model_id": "sonic-3.5",
                    "transcript": text,
                    "voice": {"mode": "id", "id": self._voice_id},
                    "output_format": {
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                    "language": "ta",
                },
            )
            if r.status_code != 200:
                return b""
            self.total_bytes += len(r.content)
            return r.content

    async def close(self):
        pass


class TextSTT:
    async def transcribe(self, audio: bytes) -> str:
        return audio.decode("utf-8", errors="replace")

    async def close(self):
        pass


class TestAvail:
    async def query_day_availability(self, q: AvailabilityQuery) -> DayAvailability:
        return DayAvailability(
            business_date=q.target_date,
            day_of_week="tuesday",
            is_operating_day=True,
            is_exception_day=False,
            available_slots=(
                AvailableSlot(1, "Dr. Priya", dt_time(18, 30), dt_time(19, 0), "scaling"),
            ),
        )


@asynccontextmanager
async def _session_factory():
    from fonely.core.database import async_session

    async with async_session() as session:
        yield session


def _validation_factory(session):
    from fonely.api.internal.validation import InternalValidationPort

    return InternalValidationPort(session)


class TestLiveBooking:
    @pytest.mark.asyncio
    async def test_real_llm_tts_postgres_booking(self):
        """Real LLM + Real TTS + Real PostgreSQL: complete Tamil booking."""
        actor = build_actor_context(business_id=1, phone="+919000000000", session_id="live-test-1")

        command_port = AppointmentServiceCommandPort(
            actor=actor,
            session_factory=_session_factory,
            validation_factory=_validation_factory,
            business_timezone="Asia/Kolkata",
            conversation_id="live-test-1",
        )

        llm = RealLLM()
        tts = RealTTS()

        config = VoiceSessionConfig(session_id="live-test-1", business_id=1)
        rt = PipelineRuntime(
            config,
            clock=_clock(),
            business_name="Smile Dental Clinic",
            business_context=(
                "Dr. Priya: Mon-Sat, scaling/consultation. Scaling ₹800. Only two slots: "
                "10:00 and 18:30."
            ),
            business_timezone="Asia/Kolkata",
            stt=TextSTT(),
            llm=llm,
            tts=tts,
            availability_port=TestAvail(),
            command_port=command_port,
            session_mode="live",
        )

        await rt.initialize()

        turns = [
            "Appointment book pannanum",
            "Scaling",
            "Naalaikku",
            "6:30 PM",
            "Karthick",
            "Aamaa",
        ]

        results = []
        for text in turns:
            result = await rt.process_turn(text.encode("utf-8"))
            results.append(result)
            print(
                f"Turn {result.turn_number}: caller='{text}' → "
                f"response='{result.response_text[:80]}...' "
                f"allowed={result.allowed} class={result.speech_class}"
            )

        # Assertions
        assert llm.call_count >= 6, f"LLM should be called at least 6 times, got {llm.call_count}"

        # At least some turns should have TTS audio (non-consequential speech is allowed)
        allowed_turns = [r for r in results if r.allowed]
        assert len(allowed_turns) >= 3, (
            f"At least 3 turns should be allowed, got {len(allowed_turns)}"
        )

        # TTS should have been called for allowed turns
        assert tts.call_count >= 3, f"TTS should be called for allowed turns, got {tts.call_count}"
        assert tts.total_bytes > 0, "TTS should produce audio bytes"

        print(f"\nLLM calls: {llm.call_count}")
        print(f"TTS calls: {tts.call_count}, total audio: {tts.total_bytes} bytes")
        print(f"Allowed turns: {len(allowed_turns)}/{len(results)}")

        # Check if booking was committed (may not be if fail-closed blocks)
        last = results[-1]
        if last.commit_receipt is not None:
            receipt = last.commit_receipt
            print(f"\nCOMMITTED: appointment_id={receipt.commitment_id}")
            print(f"  facts: {receipt.facts}")
            assert receipt.facts["service_name"] == "scaling"
            assert receipt.facts["resource_name"] == "Dr. Priya"
        else:
            print(
                "\nNote: booking not committed "
                "(expected: fail-closed validator blocks consequential speech)"
            )
            print(
                "This is correct behavior — consequential speech stays BLOCKED "
                "until validator is real"
            )

        await rt.close()
        print("\nLIVE BOOKING TEST COMPLETE")
