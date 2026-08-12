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


async def create_call_row(*, conversation_id: str) -> int:
    """Create the call record this session's notice evidence updates, and return
    its id.

    The authoritative SqlDpdpEvidenceWriter UPDATEs the dpdp_notice_* columns on
    an EXISTING call row (WHERE id=:call_id AND dpdp_notice_completed_at IS NULL)
    — it does not create the row. On the real telephony path admission creates
    the call row; the demo has no admission, so this stands in for it: one row
    per connected session, created before the notice plays, its id threaded into
    the open sequence so the evidence lands on THIS call.
    """
    from sqlalchemy import text as sql_text

    async with _SessionLocal() as session:
        row = await session.execute(
            sql_text(
                "INSERT INTO calls (business_id, caller_phone, caller_role, "
                "started_at) "
                "VALUES (:b, :phone, 'customer', now()) RETURNING id"
            ),
            {"b": DEMO_BUSINESS_ID, "phone": DEMO_CUSTOMER_PHONE},
        )
        call_id = row.scalar()
        await session.commit()
    _CALL_ROWS[conversation_id] = call_id
    logger.info("demo_call_row_created call_id=%s conv=%s", call_id, conversation_id)
    return call_id


def build_notice_evidence_writer():
    """Return the authoritative DpdpEvidenceWriter the open order writes through.

    D3 persistence half: this is the CEO's SqlDpdpEvidenceWriter (SHA2), which
    UPDATEs the four dpdp_notice_* columns on the call row inside a short
    committed transaction, so the evidence is DURABLE at the instant STT opens.
    fonely_dev4 is migrated to head 0018 (columns present), so it no longer
    raises UndefinedColumn. It is fail-loud by contract: a write failure RAISES,
    keeping capture CLOSED — the connect handler only knows the port, so this
    swap needed no handler change beyond threading a real call_id (see
    create_call_row).
    """
    from fonely.voice.evidence import SqlDpdpEvidenceWriter

    return SqlDpdpEvidenceWriter(session_factory=_session_factory)
