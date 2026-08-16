"""#43 P2 — inbound-worker-process HALF of the separate-process E2E.

The full E2E proves the two REAL processes cooperate through PostgreSQL alone:

  * this proc (inbound worker) resolves an owner's WhatsApp reply against a durable
    ``awaiting_owner_reply`` marker and CAS-flips it to ``RESUME_REQUESTED`` with a
    digest-consistent answer, then
  * Dev4's voice-runtime proc claims that ``RESUME_REQUESTED`` marker (for a call_id
    held in its LOCAL ResumeRegistry) and reconciles the answer.

A single-process test would falsely pass because both halves would share one Python
heap and one session cache; the whole point is that the ONLY thing shared between the
two procs is the row in Postgres. So this file is deliberately import-light and drives
the GENUINE ``run_inbound_worker`` path (not the service in isolation) against a shared
DB whose URL comes from the environment.

Coordination seam with Dev4 (fonely-dev4-session3):
  * Both procs point at ONE shared DB via ``DATABASE_URL`` (postgresql+asyncpg://…).
    This side CREATES + MIGRATES it to head (0021) — see ``prepare_shared_db``.
  * The one hand-off value is ``(business_id, call_id, correlation_code)``: this side
    SEEDS the marker and EMITS that triple (``seed_awaiting_marker`` returns it, and
    ``__main__`` prints it as one JSON line). Dev4's voice proc reads the triple and
    registers a matching ResumeHandle so its claim loop selects exactly this marker.

Ownership: this file lives entirely in the Dev3 lane (tests/integration/e2e). It does
NOT import fonely.voice.**, does NOT author a migration, and does NOT touch the
in-memory ResumeRegistry. It imports only the shared symbols Dev4 owns (enums, payload
+ digest helpers) and the real inbound worker.

Not run here: ``/scratch`` is at capacity and Dev4's P3 claim loop is still being
written, so the JOINT run waits on disk + P3 being testable. This half is built now so
wiring is a one-line hand-off when both are ready. Nothing in this module is imported
by product code or collected as a pytest test (filename is ``e2e_*``, not ``test_*``).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fonely.domain.pending_actions.payloads import validate_payload
from fonely.domain.pending_actions.snapshots import (
    canonical_payload_dict,
    payload_digest,
)
from fonely.models.enums import PendingActionStatus, PendingActionType
from fonely.services.model_gateway import ModelResponse
from fonely.workers.inbound_worker import run_inbound_worker

_BACKEND_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- #
# The hand-off value: what this proc seeds and what Dev4's proc must register.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SeededMarker:
    business_id: int
    call_id: int
    correlation_code: str
    action_id: int


class _RefusingGateway:
    """A ModelGateway that must never be called on the resume path.

    The owner-reply resume seam resolves BEFORE any model call, so a correct run
    never touches the provider. If it ever does, the E2E fails loudly instead of
    silently reaching a real endpoint.
    """

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> ModelResponse:
        raise AssertionError(
            "model gateway called during owner-reply resume — the seam should "
            "resolve the marker without any provider round-trip"
        )


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# DB setup — this side owns creating + migrating the shared DB to head (0021).
# --------------------------------------------------------------------------- #
def prepare_shared_db(database_url: str) -> None:
    """Migrate the shared DB to head using the real Alembic config.

    Both procs point here via ``DATABASE_URL``. Migrating (not create_all) is what
    the CLAUDE.md migration policy requires and is what Dev4's proc also expects —
    the schema must be the genuine 0021 head, not an ORM snapshot. Alembic's env.py
    reads ``settings.database_url`` (bound from ``DATABASE_URL``).
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    proc = subprocess.run(
        [str(_BACKEND_ROOT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=_BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Surface stdout/stderr so a migration failure never reads as a product bug.
        raise RuntimeError(
            "alembic upgrade head failed for the shared E2E DB "
            f"(returncode={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )


def make_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """The app-shape session factory both the seed and the worker use.

    ``expire_on_commit=False`` mirrors production (fonely/app.py) so post-commit
    reads don't re-issue queries against a closed transaction.
    """
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


# --------------------------------------------------------------------------- #
# Seed: business + owner + call + a real digest-consistent AWAITING marker.
# --------------------------------------------------------------------------- #
async def seed_awaiting_marker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    business_id: int = 1,
    correlation_code: str = "AB23",
    query_type: str = "availability",
    expires_in_minutes: int = 60,
) -> SeededMarker:
    """Seed the durable wait exactly as Dev4's P1 escalation would.

    Returns the ``(business_id, call_id, correlation_code, action_id)`` hand-off so
    the voice proc can register a matching ResumeHandle for the SAME (business_id,
    call_id) — the one coordination point Dev4 called out.
    """
    async with session_factory() as session:
        owner_phone = f"+9190000000{business_id:02d}"
        await session.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription) "
                "VALUES (:id, :name, 'dental', :phone, 'Asia/Kolkata', 'trial') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": business_id, "name": f"Clinic {business_id}", "phone": owner_phone},
        )
        await session.execute(
            text(
                "INSERT INTO business_users (business_id, phone, role, is_active) "
                "VALUES (:bid, :phone, 'owner', true) ON CONFLICT DO NOTHING"
            ),
            {"bid": business_id, "phone": owner_phone},
        )
        row = await session.execute(
            text(
                "INSERT INTO calls (business_id, caller_phone, caller_role, started_at) "
                "VALUES (:bid, '+919000000000', 'customer', now()) RETURNING id"
            ),
            {"bid": business_id},
        )
        call_id = int(row.scalar_one())

        # The marker payload the way P1 does: a validated envelope whose canonical
        # form + digest agree, so a validated read succeeds until the answer CAS
        # deliberately keeps them consistent.
        envelope = validate_payload(
            PendingActionType.AWAITING_OWNER_REPLY,
            1,
            {
                "schema_version": 1,
                "action_type": "awaiting_owner_reply",
                "data": {
                    "answer_text": None,
                    "answered_by_phone": None,
                    "answered_at": None,
                    "answer_unused": False,
                },
            },
        )
        payload = json.dumps(canonical_payload_dict(envelope))
        marker_row = await session.execute(
            text(
                "INSERT INTO pending_actions "
                "(business_id, action_type, payload_schema_version, proposed_payload, "
                " payload_digest, status, expires_at, idempotency_key, initiated_by, "
                " call_id, query_type, correlation_code, version) "
                "VALUES (:bid, 'awaiting_owner_reply', 1, CAST(:payload AS jsonb), "
                " :digest, :status, now() + make_interval(mins => :mins), :idem, "
                " '+919000000000', :call_id, :qt, :code, 1) RETURNING id"
            ),
            {
                "bid": business_id,
                "payload": payload,
                "digest": payload_digest(envelope),
                "status": PendingActionStatus.AWAITING_OWNER_REPLY.value,
                "mins": expires_in_minutes,
                "idem": f"e2e-marker-{business_id}-{call_id}-{correlation_code}",
                "call_id": call_id,
                "qt": query_type,
                "code": correlation_code,
            },
        )
        action_id = int(marker_row.scalar_one())
        await session.commit()

    return SeededMarker(
        business_id=business_id,
        call_id=call_id,
        correlation_code=correlation_code,
        action_id=action_id,
    )


async def seed_owner_reply_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    business_id: int,
    correlation_code: str,
    message_body: str,
) -> int:
    """Insert the claimable inbound WhatsApp event carrying the owner's coded reply.

    The sender is the SEEDED OWNER phone so ``_is_owner`` is true and the worker
    enters the resume seam. The body is the owner's answer WITH the correlation code
    (so >1-marker disambiguation is exercised when the harness seeds multiple), which
    the service strips before persisting. Shape matches the durable-inbox contract
    the real claim loop selects on.
    """
    owner_phone = f"9190000000{business_id:02d}"  # digits only; worker normalizes +.
    async with session_factory() as session:
        row = await session.execute(
            text(
                "INSERT INTO whatsapp_inbound_events "
                "(message_id, business_id, phone_number_id, sender_phone, "
                " message_type, message_body, status, attempts, max_attempts, "
                " provider_timestamp, next_attempt_at) "
                "VALUES (:mid, :bid, :pnid, :sender, 'text', :body, 'received', 0, 5, "
                " now(), now()) RETURNING id"
            ),
            {
                "mid": f"e2e-owner-reply-{business_id}-{correlation_code}",
                "bid": business_id,
                "pnid": f"phone-{business_id}",
                "sender": owner_phone,
                "body": message_body,
            },
        )
        event_id = int(row.scalar_one())
        await session.commit()
    return event_id


async def read_marker(
    session_factory: async_sessionmaker[AsyncSession], action_id: int
) -> tuple[str, dict]:
    """(status, payload['data']) for the seeded marker — the proc-crossing evidence."""
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, proposed_payload FROM pending_actions WHERE id = :id"
                ),
                {"id": action_id},
            )
        ).one()
        return row.status, dict(row.proposed_payload["data"])


async def validated_read_ok(
    session_factory: async_sessionmaker[AsyncSession], action_id: int
) -> bool:
    """Prove P3's digest-validating read accepts the answered marker.

    This is exactly the check ``_validated_stored_payload`` runs. If the answer CAS
    had left the digest stale, this raises — the cross-lane integration trap. A
    ``True`` here means Dev4's proc can read the answer without a digest mismatch.
    """
    async with session_factory() as session:
        action = (
            await session.execute(
                text(
                    "SELECT action_type, payload_schema_version, proposed_payload, "
                    "payload_digest FROM pending_actions WHERE id = :id"
                ),
                {"id": action_id},
            )
        ).one()
        payload = validate_payload(
            PendingActionType(action.action_type),
            action.payload_schema_version,
            action.proposed_payload,
        )
        return payload_digest(payload) == action.payload_digest


# --------------------------------------------------------------------------- #
# The inbound-side run: seed the reply event, drive the REAL worker, verify.
# --------------------------------------------------------------------------- #
async def run_inbound_side(
    database_url: str,
    *,
    seeded: SeededMarker,
    message_body: str,
    max_iterations: int = 3,
) -> SeededMarker:
    """Drive the genuine inbound worker until it flips the seeded marker.

    Given an ALREADY-SEEDED marker (so the caller controls the (business_id,
    call_id, code) hand-off), enqueue the owner's coded reply and run the real
    ``run_inbound_worker`` for a bounded number of iterations. Asserts, purely from
    the DB, that the marker moved to RESUME_REQUESTED with a digest-consistent
    answer that P3's validated read accepts — then leaves it for the voice proc.
    """
    session_factory = make_session_factory(database_url)

    await seed_owner_reply_event(
        session_factory,
        business_id=seeded.business_id,
        correlation_code=seeded.correlation_code,
        message_body=message_body,
    )

    # The REAL worker path: claim → lease → _process_domain (owner-reply seam) →
    # enqueue idempotent response → mark processed → commit. Bounded so the E2E
    # terminates; the gateway must never be reached on this path.
    await run_inbound_worker(
        session_factory,
        _RefusingGateway(),
        max_iterations=max_iterations,
    )

    status, data = await read_marker(session_factory, seeded.action_id)
    if status != PendingActionStatus.RESUME_REQUESTED.value:
        raise AssertionError(
            f"marker {seeded.action_id} expected RESUME_REQUESTED, got {status!r} — "
            "the inbound worker did not resolve the owner reply"
        )
    if not data.get("answer_text"):
        raise AssertionError("resolved marker has no persisted answer_text")
    if not await validated_read_ok(session_factory, seeded.action_id):
        raise AssertionError(
            "answered marker fails the digest-validating read — the answer CAS left "
            "payload_digest stale (P3 would reject every answered marker)"
        )
    return seeded


async def _amain() -> int:
    """Standalone inbound-side smoke: create-free (DB must pre-exist), seed, run.

    Requires ``DATABASE_URL`` (postgresql+asyncpg) pointing at the shared DB and
    ``FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1`` as an explicit "this is a throwaway DB"
    guard. Prints ONE JSON line with the hand-off triple so Dev4's voice proc reads
    it: ``{"business_id":1,"call_id":…,"correlation_code":"AB23","action_id":…}``.
    Does NOT create/migrate here by default — the joint runner calls
    ``prepare_shared_db`` once so both procs agree on schema before either seeds.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if "+asyncpg" not in database_url:
        print("DATABASE_URL must be a postgresql+asyncpg URL", file=sys.stderr)
        return 2
    if os.environ.get("FONELY_ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        print(
            "refusing to run: set FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 to confirm this "
            "is a throwaway E2E DB",
            file=sys.stderr,
        )
        return 2

    session_factory = make_session_factory(database_url)
    seeded = await seed_awaiting_marker(session_factory)
    result = await run_inbound_side(
        database_url,
        seeded=seeded,
        message_body=f"code: {seeded.correlation_code} yes she is free at 6:30",
    )
    # The hand-off line the voice proc consumes (single JSON object, stdout).
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
