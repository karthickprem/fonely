"""Bridge the demo server to the PRODUCTION voice package (backend/src).

The demo must run the code that ships, not a lab copy. This module constructs
the production ResolverContext + FrameProcessors from fonely.voice, pointed at
the seeded fonely_dev4 demo clinic, and exposes the same doctor-bridge the
server panel talks to.

Nothing about the conversation logic lives here — it's pure wiring. The
processors, resolver, single-commit path, and language mirroring are all
imported from backend/src/fonely/voice.
"""
from __future__ import annotations

import os
import sys

# Production package on the path.
_RUNTIME_SRC = "/scratch/karthick/fonely/.claude/worktrees/dev4-voice-runtime/backend/src"
if _RUNTIME_SRC not in sys.path:
    sys.path.insert(0, _RUNTIME_SRC)

# The demo runs against the seeded fonely_dev4 clinic (6 services, 2 doctors,
# split shifts) — NOT the poisoned `fonely` DB. Set before any fonely import
# so Settings and the engine pick it up.
_DEV4_DB = "postgresql+asyncpg://localhost:5432/fonely_dev4"
os.environ.setdefault("DATABASE_URL", _DEV4_DB)
os.environ.setdefault("INTERNAL_API_SECRET", "dev4-demo-secret")
os.environ.setdefault("WHATSAPP_BUSINESS_MAPPINGS", '{"demo-phone-id": 1}')

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from fonely.voice.frame_pipeline import (
    BookingStateInjector,
    BookingPostLLMGate,
    ResolverContext,
)
from fonely.voice.backend_ports import AppointmentServiceCommandPort, build_actor_context
from fonely.voice.context import TrustedClock
from fonely.voice.doctor_bridge import InMemoryDoctorBridge

# The demo clinic seeded via the supported onboarding route.
DEMO_BUSINESS_ID = 1
DEMO_TIMEZONE = "Asia/Kolkata"
DEMO_CUSTOMER_PHONE = "+919840000042"

# The demo ALWAYS runs against fonely_dev4. Bind explicitly rather than
# trusting the ambient env — a SQLite default from fonely.core.config kept
# leaking in through the server's import chain and producing a bogus
# "postgresql+asyncpg:///./fonely.db" URL. Hardcoding the demo DB removes that
# whole class of failure; this is demo wiring, not shippable config.
_url = _DEV4_DB
import logging as _logging
logger = _logging.getLogger("fonely.voice.production_wiring")
logger.info("demo DB engine → %s", _url)
_engine = create_async_engine(_url, pool_size=5)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)

# One doctor bridge shared with the server's owner panel.
BRIDGE = InMemoryDoctorBridge(timezone=DEMO_TIMEZONE)


def _session_factory():
    return _SessionLocal()


def _validation_factory(session):
    from fonely.api.internal.validation import InternalValidationPort
    return InternalValidationPort(session)


def build_processors(conversation_id: str):
    """Construct the production injector + gate for one call.

    Returns (injector, gate). The pipeline slots these in exactly where the
    lab copies used to sit — same positions, production code.
    """
    actor = build_actor_context(
        business_id=DEMO_BUSINESS_ID,
        phone=DEMO_CUSTOMER_PHONE,
        session_id=conversation_id,
    )
    command_port = AppointmentServiceCommandPort(
        actor=actor,
        session_factory=_session_factory,
        validation_factory=_validation_factory,
        business_timezone=DEMO_TIMEZONE,
        conversation_id=conversation_id,
    )

    async def ask_doctor(question: str, patient_context: str = ""):
        await BRIDGE.ask_doctor(question, patient_context)

    resolver = ResolverContext(
        business_id=DEMO_BUSINESS_ID,
        session_factory=_session_factory,
        command_port=command_port,
        clock=TrustedClock.from_now(DEMO_TIMEZONE),
        ask_doctor=ask_doctor,
    )

    injector = BookingStateInjector(resolver)
    gate = BookingPostLLMGate(injector, resolver)
    return injector, gate


async def clinic_context_text() -> str:
    """Live clinic context from the production resolver, for the owner panel /
    the injector's context injection."""
    from fonely.voice import clinic_resolver
    async with _SessionLocal() as session:
        return await clinic_resolver.clinic_context_text(session, DEMO_BUSINESS_ID)


# conversation_id → call row id, so the notice evidence and later turns attach
# to the same call record.
_CALL_ROWS: dict[str, int] = {}


class _TranscriptEvidenceWriter:
    """A DpdpEvidenceWriter (fonely.voice.evidence port) backed by the current
    demo DB schema.

    D3 has two halves. The STRUCTURE half (this): the open order writes evidence
    through the port, never a raw INSERT inline in the connect handler — so the
    real SqlDpdpEvidenceWriter drops in unchanged when it lands. The PERSISTENCE
    half (pending): the authoritative dpdp_notice_* columns don't exist on this
    database yet — fonely_dev4 is at migration head 0015, those columns land at
    0018. A writer targeting them would raise UndefinedColumn here. So until the
    DB is migrated forward and the CEO's SqlDpdpEvidenceWriter integrates, this
    writes the notice-completion event into the existing calls.transcript JSONB
    (the only place the current schema can hold it).

    Crucially it is FAIL-LOUD: a write failure RAISES (the port contract), so the
    open order keeps capture CLOSED. The demo's old record_notice_evidence
    swallowed the error in a warning and let the call proceed — that was the
    false-consent defect. This does not swallow.
    """

    def __init__(self, *, conversation_id: str) -> None:
        self._conversation_id = conversation_id

    async def write(
        self,
        *,
        call_id: int,
        completed_at,
        notice_version: str,
        locale: str,
        content_digest: str,
    ) -> None:
        import json as _json

        from sqlalchemy import text as sql_text

        # The notice-completion evidence event. Includes the content digest so
        # the same tamper-evident value the authoritative column will hold is
        # recorded now, alongside the human-readable marker.
        event = {
            "role": "system",
            "kind": "dpdp_notice",
            "notice_version": notice_version,
            "locale": locale,
            "content_digest": content_digest,
            "completed_at": completed_at.isoformat(),
        }
        async with _SessionLocal() as session:
            row = await session.execute(
                sql_text(
                    "INSERT INTO calls (business_id, caller_phone, caller_role, "
                    "transcript, started_at) "
                    "VALUES (:b, :phone, 'customer', :tr, now()) RETURNING id"
                ),
                {
                    "b": DEMO_BUSINESS_ID,
                    "phone": DEMO_CUSTOMER_PHONE,
                    "tr": _json.dumps([event]),
                },
            )
            row_id = row.scalar()
            await session.commit()
        _CALL_ROWS[self._conversation_id] = row_id
        logger.info(
            "dpdp_notice_recorded call_id=%s version=%s digest=%s",
            row_id,
            notice_version,
            content_digest[:12],
        )


def build_notice_evidence_writer(*, conversation_id: str):
    """Return the DpdpEvidenceWriter the open order writes notice evidence
    through.

    Swap seam: when fonely_dev4 is migrated to the 0018 head and the CEO's
    SqlDpdpEvidenceWriter integrates, this returns that writer (targeting the
    authoritative dpdp_notice_* columns) instead of the transcript-backed one —
    no change to the connect handler, which only knows the port.
    """
    return _TranscriptEvidenceWriter(conversation_id=conversation_id)
