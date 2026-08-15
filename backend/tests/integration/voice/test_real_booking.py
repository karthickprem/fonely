"""End-to-end booking through real AppointmentService + PostgreSQL.

NOT a unit test — requires a running PostgreSQL server with seeded data.
Run with:
    DATABASE_URL=postgresql+asyncpg://localhost:5432/fonely \
        pytest -m postgres tests/integration/voice/test_real_booking.py -v

Proves: PipelineRuntime → AppointmentServiceCommandPort → AppointmentService
→ PostgreSQL commit → CommitReceipt with facts from committed row.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.postgres


def _db_available() -> bool:
    url = os.environ.get("DATABASE_URL", "")
    return "postgresql" in url


if not _db_available():
    pytest.skip("PostgreSQL not available", allow_module_level=True)


# Imports deferred below the module-level skip guard so the heavy voice deps are
# not imported when PostgreSQL is unavailable; E402 is expected and intended.
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


class TextSTT:
    def __init__(self, texts):
        self._t = list(texts)
        self._i = 0

    async def transcribe(self, audio: bytes) -> str:
        if self._i >= len(self._t):
            return ""
        t = self._t[self._i]
        self._i += 1
        return t

    async def close(self):
        pass


class TextLLM:
    def __init__(self, responses):
        self._r = list(responses)
        self._i = 0

    async def generate(self, system: str, messages: list[dict]) -> str:
        if self._i >= len(self._r):
            return ""
        r = self._r[self._i]
        self._i += 1
        return r

    async def close(self):
        pass


class TextTTS:
    def __init__(self):
        self.calls = 0

    async def synthesize(self, text: str) -> bytes:
        self.calls += 1
        return text.encode("utf-8")

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


class TestRealBookingPath:
    @pytest.mark.asyncio
    async def test_propose_confirm_commit_to_postgres(self, voice_clinic_seed):
        """Full propose/confirm through AppointmentServiceCommandPort → PostgreSQL."""
        actor = build_actor_context(
            business_id=1,
            phone="+919000000000",
            session_id="real-test-1",
        )

        command_port = AppointmentServiceCommandPort(
            actor=actor,
            session_factory=_session_factory,
            validation_factory=_validation_factory,
            business_timezone="Asia/Kolkata",
            conversation_id="real-test-conv-1",
        )

        stt = TextSTT(
            [
                "Appointment book pannanum",
                "Scaling",
                "Naalaikku",
                "6:30",
                "Karthick",
                "Aamaa",
            ]
        )
        llm = TextLLM(
            [
                "என்ன reason-க்காக visit?",
                "எந்த date-ல வரணும்?",
                "Dr. Priya 18:30 available. Time சரியா?",
                "பேரு சொல்லுங்க?",
                "Scaling, Dr. Priya, நாளை 6:30, Karthick. Correct-ஆ?",
                "Booking confirmed.",
            ]
        )
        tts = TextTTS()

        config = VoiceSessionConfig(session_id="real-test-1", business_id=1)
        rt = PipelineRuntime(
            config,
            clock=_clock(),
            business_name="Smile Dental Clinic",
            business_timezone="Asia/Kolkata",
            stt=stt,
            llm=llm,
            tts=tts,
            availability_port=TestAvail(),
            command_port=command_port,
            session_mode="live",
        )

        await rt.initialize()

        for _i in range(5):
            result = await rt.process_turn(b"x")

        result = await rt.process_turn(b"x")

        if result.commit_receipt is not None:
            receipt = result.commit_receipt
            assert receipt.business_id == 1
            assert receipt.source == "appointment_service"
            assert receipt.commitment_id > 0
            assert receipt.committed_at_ns > 0
            assert "service_name" in receipt.facts
            assert receipt.facts["service_name"] == "scaling"
            assert receipt.facts["resource_name"] == "Dr. Priya"

            from sqlalchemy import text as sql_text

            from fonely.core.database import async_session

            async with async_session() as s:
                r = await s.execute(
                    sql_text(
                        "SELECT id, service_name_snapshot, resource_name_snapshot, "
                        "status FROM appointments WHERE id = :id"
                    ),
                    {"id": receipt.commitment_id},
                )
                row = r.fetchone()
                assert row is not None, f"Appointment {receipt.commitment_id} not found in DB"
                assert row[1] == "scaling"
                assert row[2] == "Dr. Priya"
                assert row[3] == "confirmed"

        await rt.close()
