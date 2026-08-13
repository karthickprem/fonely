"""Adversarial + concurrency hardening for the durable offer path (_active_offer).

offers.py's PURE validation is already well-covered in tests/unit/booking/test_offers.py
(cross_tenant, cross_conversation, expired, invalid_token, tampered_token,
naive_datetime, invalid_expiry, malformed->None). This file targets the GAPS that
live in the INTEGRATION + CONCURRENCY layer, which single-object unit tests cannot
reach:

  1. COMMIT-LEVEL RACE — two conversations each holding a VALID offer for the SAME
     slot, both accepting. Exactly one appointment commits; the loser gets the
     specific truthful "resource_unavailable", no double-book, AND no orphan
     selection state left behind (clean loss -> re-offerable, not stranded).
  2. STALE-via-SUPERSEDE — a superseded offer's token is refused by the ACTUAL
     mechanism (offer_id replacement + HMAC binding), not by the vestigial
     `revision` field. See the revision note below.
  3. PERSISTENCE-FAILURE — an offer half-written into collected_facts must not let
     a later turn read a coherent-looking booking; fail closed.
  4. PER-FIELD HMAC tampering — the token binds six fields; each flipped
     independently must be rejected as tampered_token.
  5. ttl_exceeded / too_many_slots — deserialize/build bounds, mutation-proven.

REVISION IS VESTIGIAL — LATENT TRAP, flagged not fixed. build_offer hardcodes
revision=1; nothing ever bumps it; SelectedSlot.offer_revision is never compared
on accept. Supersession is enforced entirely by offer_id replacement + the HMAC
token binding (a superseded offer has a new offer_id, so old tokens fail
find_by_token, and the HMAC binds offer_id so a token cannot be replayed onto a
new offer). A future dev must NOT remove the offer_id/HMAC mechanism assuming
`revision` protects supersession — it does not. Adding a revision check would be a
redundant guard over an already-locked door; deliberately not added.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Import-origin assertion (venv discipline): this test must exercise WORKTREE src,
# not the main-checkout src the shared .venv resolves without PYTHONPATH.
import fonely.domain.booking.offers as _offers_mod
from fonely.api.internal.validation import (
    AppointmentAvailabilityError,
    InternalValidationPort,
)
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.appointments.results import (
    PreCommitAppointmentFailure,
    PreCommitAppointmentSuccess,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole, Channel
from fonely.models.schema import Appointment
from fonely.services.appointments import AppointmentService
from fonely.services.availability import AvailabilityReason
from tests.integration.postgres.concurrency import install_transaction_timeouts
from tests.integration.postgres.conftest import seed_whatsapp_channel

assert "/dev3-dental-e2e/" in _offers_mod.__file__, (
    f"offers.py resolved from {_offers_mod.__file__!r} — not the worktree; "
    "run with PYTHONPATH=$PWD/src or the test exercises stale code"
)

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
    )


def _slot_start() -> datetime:
    day = datetime.now(KOLKATA).date() + timedelta(days=2)
    return datetime.combine(
        day, datetime.min.time().replace(hour=10, minute=0), tzinfo=KOLKATA
    ).astimezone(UTC)


async def _seed(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription, "
            "appointment_slot_interval_minutes) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', '+919000000001', "
            "'Asia/Kolkata', 'trial', 30)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (id, business_id, phone, role, is_active) "
            "VALUES (1, 1, '+919000000001', 'owner', true)"
        )
    )
    await seed_whatsapp_channel(session, phone_number_id="phone-1")
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) VALUES (1, 1, 1, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '09:00', '18:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


async def _make_proposal(
    factory: async_sessionmaker[AsyncSession], start: datetime, key: str
) -> tuple[int, int]:
    """Create a confirmed-availability proposal for `start` — the state a caller
    reaches by naming an offered slot. Returns (pending_action_id, version)."""
    async with factory() as session:
        service = AppointmentService(session, validation=InternalValidationPort(session))
        proposal = await service.create_proposal(
            CreatePendingAppointmentCommand(
                actor=_actor(),
                service_id=1,
                resource_id=1,
                start_at=start,
                customer_phone=_actor().normalized_phone,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                idempotency_key=key,
            )
        )
        await session.commit()
        return proposal.pending_action_id, proposal.version


def _is_clean_loss(outcome: object) -> bool:
    """A losing confirm is 'clean' when the caller is told, truthfully and
    specifically, that the slot is gone AND the conversation layer re-offers.

    Two rejection forms both qualify — and conversation._confirm_booking
    (services/conversation.py) maps BOTH to the identical 're-offer another
    time' turn, so treating them as one is not a fudge, it is the product
    contract:
      * AppointmentAvailabilityError(capacity_conflict) — the loser's
        begin_commit revalidation, run WHILE HOLDING the resource FOR UPDATE
        lock, re-reads occupancy after the winner committed and sees the slot
        taken. This is the serialized layer and it is what fires in the real
        race. conversation.py catches it under `except (…, ValueError)`.
      * PreCommitAppointmentFailure(resource_unavailable) — the GiST overlap
        exclusion constraint at insert. The SECOND backstop; fires only if the
        serialized revalidation is bypassed (see the lock-neutralized mutation).
    A raw crash / any other exception is NOT a clean loss.
    """
    if isinstance(outcome, PreCommitAppointmentFailure):
        return outcome.error_code == "resource_unavailable"
    if isinstance(outcome, AppointmentAvailabilityError):
        return outcome.reason is AvailabilityReason.CAPACITY_CONFLICT
    return False


async def _race_two_confirms(
    factory: async_sessionmaker[AsyncSession],
    pa_a: int,
    ver_a: int,
    pa_b: int,
    ver_b: int,
) -> tuple[object, object]:
    async def confirm(pa_id: int, version: int) -> object:
        async with factory() as session:
            await install_transaction_timeouts(session)
            service = AppointmentService(session, validation=InternalValidationPort(session))
            try:
                outcome = await service.confirm_and_commit(
                    ConfirmPendingAppointmentCommand(
                        actor=_actor(),
                        pending_action_id=pa_id,
                        expected_version=version,
                    )
                )
            except Exception as exc:  # the race's rejection IS the datum
                await session.rollback()
                return exc
            await session.commit()
            return outcome

    outcome_a, outcome_b = await asyncio.gather(confirm(pa_a, ver_a), confirm(pa_b, ver_b))
    return outcome_a, outcome_b


async def test_two_valid_offers_same_slot_exactly_one_commits(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIORITY 1 — the commit-level race, the real untested durable-offer surface.

    Two conversations each hold a VALID proposal (the state a caller reaches by
    accepting an offered slot) for the SAME resource+slot. Both confirm
    concurrently.

    THE MECHANISM (grounded in services/appointments.py + pending_actions.py):
    confirm_and_commit takes `SELECT 1 FROM resources … FOR UPDATE` on the
    resource row FIRST, then runs begin_commit, whose availability revalidation
    re-reads occupancy. Because both racers contend on that one resource-row
    lock, they SERIALIZE: the winner commits its appointment+allocation and
    releases; the loser only then acquires the lock, and its revalidation —
    under READ COMMITTED, a fresh per-statement snapshot — now sees the winner's
    committed allocation and returns capacity_conflict. The guarantee therefore
    comes from LOCK-ORDERED SERIALIZATION, not a pre-check that both racers
    could pass before either writes (no TOCTOU window). The GiST exclusion
    constraint is a proven SECOND backstop (see the lock-neutralized mutation
    below), not the primary guard here.

    Assert the invariants that hold regardless of which layer fires:
      - exactly ONE appointment row (no double-book) — the load-bearing invariant,
      - exactly one PreCommitAppointmentSuccess (a real winner),
      - exactly one CLEAN loss (truthful 'slot gone' → re-offerable), not a crash,
      - NO orphan booking state from the loser (exactly one active allocation).
    """
    async with pg_session_factory() as setup:
        await _seed(setup)
    start = _slot_start()
    pa_a, ver_a = await _make_proposal(pg_session_factory, start, "offer-race-a")
    pa_b, ver_b = await _make_proposal(pg_session_factory, start, "offer-race-b")

    outcome_a, outcome_b = await _race_two_confirms(pg_session_factory, pa_a, ver_a, pa_b, ver_b)

    outcomes = [outcome_a, outcome_b]
    successes = [o for o in outcomes if isinstance(o, PreCommitAppointmentSuccess)]
    losses = [o for o in outcomes if _is_clean_loss(o)]

    assert len(successes) == 1, f"expected exactly one winner, got {outcomes!r}"
    assert len(losses) == 1, (
        f"expected exactly one CLEAN loss, got {outcomes!r} — anything other than a "
        "truthful 'slot no longer available' (capacity_conflict OR "
        "resource_unavailable) means the race is not handled gracefully"
    )

    async with pg_session_factory() as verify:
        appt_count = await verify.scalar(select(func.count(Appointment.id)))
        assert appt_count == 1, f"DOUBLE-BOOK: {appt_count} appointments for one slot"
        # No orphan allocation from the losing attempt — a clean loss, re-offerable.
        active_allocs = await verify.scalar(
            text("SELECT count(*) FROM resource_allocations WHERE status = 'active'")
        )
        assert active_allocs == 1, (
            f"loser left an orphan allocation: {active_allocs} active (expected 1)"
        )


async def test_race_lock_neutralized_gist_backstop_still_prevents_double_book(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRIORITY 1 — ANCHORED MUTATION PROOF for the serialization guarantee.

    Neutralize the guard: make lock_resource_schedule a no-op, so the two
    confirmers NO LONGER serialize on the resource row. Now both revalidations
    can run concurrently against the same pre-winner snapshot — the exact TOCTOU
    the FOR UPDATE lock exists to close — and both can believe the slot is free.

    The point of the mutation is twofold:
      1. It NAMES the consequence of removing the lock: the primary
         (serialized-revalidation) rejection can vanish, so BOTH racers reach
         the insert. If the ONLY guard were that revalidation, this is where a
         double-book would appear.
      2. It PROVES the second backstop is real: the GiST exclusion constraint
         (ex_resource_allocations_active_overlap) still catches the loser at
         insert, so there is STILL exactly one appointment and the loser STILL
         gets the truthful resource_unavailable. The invariant 'never two
         appointments for one slot' survives even with the lock gone.

    Without this mutation, a green priority-1 test could not distinguish 'the
    lock serialized them' from 'they happened not to overlap in time'. With it,
    we show the lock's causal role AND that the constraint is a genuine floor.
    """
    from fonely.repositories import appointments as _appt_repo

    async def _noop_lock(self: object, business_id: int, resource_id: int) -> None:
        return None

    monkeypatch.setattr(_appt_repo.AppointmentRepository, "lock_resource_schedule", _noop_lock)

    async with pg_session_factory() as setup:
        await _seed(setup)
    start = _slot_start()
    pa_a, ver_a = await _make_proposal(pg_session_factory, start, "offer-race-nolock-a")
    pa_b, ver_b = await _make_proposal(pg_session_factory, start, "offer-race-nolock-b")

    outcome_a, outcome_b = await _race_two_confirms(pg_session_factory, pa_a, ver_a, pa_b, ver_b)
    outcomes = [outcome_a, outcome_b]

    # The load-bearing invariant is UNCONDITIONAL: even with serialization gone,
    # the GiST constraint forbids a second overlapping active allocation.
    async with pg_session_factory() as verify:
        appt_count = await verify.scalar(select(func.count(Appointment.id)))
        assert appt_count == 1, (
            f"DOUBLE-BOOK with lock neutralized: {appt_count} appointments — the "
            "GiST exclusion backstop failed to hold the invariant the lock's "
            "serialization normally enforces"
        )
        active_allocs = await verify.scalar(
            text("SELECT count(*) FROM resource_allocations WHERE status = 'active'")
        )
        assert active_allocs == 1, (
            f"lock-neutralized race left {active_allocs} active allocations (expected 1)"
        )

    successes = [o for o in outcomes if isinstance(o, PreCommitAppointmentSuccess)]
    losses = [o for o in outcomes if _is_clean_loss(o)]
    assert len(successes) == 1, f"expected one winner even without the lock, got {outcomes!r}"
    assert len(losses) == 1, (
        f"expected one clean loss carried by the GiST backstop, got {outcomes!r}"
    )


# ---------------------------------------------------------------------------
# JSONB round-trip helpers — priorities 2-5 exercise the DURABLE path: an offer
# serialized into conversations.collected_facts (a real PG JSONB column),
# persisted, read back, and re-validated. This is the integration surface the
# pure-dict unit tests in tests/unit/booking/test_offers.py cannot reach: it
# proves the HMAC-bound fields survive an actual asyncpg JSONB encode/decode and
# that validation runs against what Postgres returns, not an in-process dict.
# ---------------------------------------------------------------------------

CONVERSATION_ID = "conv-offer-hardening-0001"


async def _seed_conversation(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO conversations "
            "(id, business_id, customer_phone, state, collected_facts, expires_at) "
            "VALUES (:id, 1, '+919123456789', 'fact_collection', '{}'::jsonb, "
            "now() + interval '1 hour')"
        ),
        {"id": CONVERSATION_ID},
    )
    await session.commit()


def _build_offer(start: datetime) -> object:
    """One valid single-slot offer for the seeded resource+service, bound to the
    seeded business+conversation — the object the orchestrator persists."""
    from fonely.domain.booking.offers import build_offer

    return build_offer(
        business_id=1,
        conversation_id=CONVERSATION_ID,
        service_id=1,
        service_name="General Consultation",
        resource_id=1,
        resource_name="Dr. Priya",
        target_date=start.astimezone(KOLKATA).date().isoformat(),
        available_slots=[{"start_at": start, "end_at": start + timedelta(minutes=30)}],
        business_timezone="Asia/Kolkata",
    )


async def _store_offer(
    factory: async_sessionmaker[AsyncSession], offer_dict: dict[str, object]
) -> None:
    """Write a serialized offer under collected_facts._active_offer, the exact
    JSONB shape the conversation path stores, and COMMIT so the next read is a
    genuine round-trip through Postgres."""
    import json

    async with factory() as session:
        await session.execute(
            text(
                "UPDATE conversations "
                "SET collected_facts = jsonb_set(collected_facts, '{_active_offer}', "
                "CAST(:offer AS jsonb)) WHERE id = :id"
            ),
            {"offer": json.dumps(offer_dict), "id": CONVERSATION_ID},
        )
        await session.commit()


async def _read_active_offer(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    async with factory() as session:
        raw = await session.scalar(
            text("SELECT collected_facts -> '_active_offer' FROM conversations WHERE id = :id"),
            {"id": CONVERSATION_ID},
        )
    assert isinstance(raw, dict), f"expected a dict back from JSONB, got {type(raw)!r}"
    return raw


def _rebuild_with_offer_id(serialized: dict[str, object], new_offer_id: str) -> dict[str, object]:
    """Return a copy of a serialized offer re-issued under `new_offer_id`, with
    EVERY slot token recomputed for that id — identical in all other respects
    (slot times, expires_at, resource/service, revision). This is what a fresh
    build_offer yields on re-derivation, but constructed from ONE source offer so
    that only offer_id varies. Isolating that single variable is what lets the
    supersede test attribute a rejection to offer_id and nothing else.
    """
    import copy

    from fonely.domain.booking.contract import AvailabilityOffer

    rebuilt = copy.deepcopy(serialized)
    rebuilt["offer_id"] = new_offer_id
    expires_at = datetime.fromisoformat(str(rebuilt["expires_at"]))
    resource_id = int(rebuilt["resource_id"])  # type: ignore[call-overload, arg-type]
    service_id = int(rebuilt["service_id"])  # type: ignore[call-overload, arg-type]
    slots = rebuilt["slots"]
    assert isinstance(slots, list)
    for slot in slots:
        assert isinstance(slot, dict)
        start = datetime.fromisoformat(str(slot["start_at_utc"]))
        end = datetime.fromisoformat(str(slot["end_at_utc"]))
        slot["token"] = AvailabilityOffer.generate_token(
            new_offer_id, start, end, resource_id, service_id, expires_at
        )
    return rebuilt


def _validate_from_stored(offer_dict: dict[str, object], token: str) -> object:
    """Deserialize a stored offer and validate a token against it — the durable
    accept path (orchestrator.validate_token_selection), business/conversation
    fixed to the seeded identity."""
    from fonely.domain.booking.offers import (
        OfferValidationError,
        deserialize_offer,
        validate_selection,
    )

    offer = deserialize_offer(offer_dict)
    if offer is None:
        raise OfferValidationError("malformed_offer", "Cannot deserialize offer")
    return validate_selection(offer, token, business_id=1, conversation_id=CONVERSATION_ID)


async def test_superseded_offer_token_refused_via_offer_id_not_revision(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIORITY 2 — STALE-via-SUPERSEDE, through the real JSONB round-trip.

    A caller is offered slots (offer A), then something re-derives availability
    and REPLACES _active_offer with offer B for the same slot. The caller then
    tries to accept using A's token. It must be refused — the durable path must
    not honour a token from an offer that is no longer the active one.

    THE ACTUAL MECHANISM: supersession is enforced by offer_id + HMAC, NOT by
    the `revision` field. build_offer mints a FRESH offer_id per call and the
    token HMAC binds offer_id (contract.generate_token). So A's token, recomputed
    against B's offer_id during B's deserialize, does not match B's stored tokens
    → the token is simply not found in B → invalid_token.

    >>> REVISION IS A VESTIGIAL LATENT TRAP (flagged, deliberately NOT fixed) <<<
    build_offer hardcodes revision=1; nothing bumps it; SelectedSlot.offer_revision
    is never compared on accept. In THIS test both A and B carry revision=1 — so
    if `revision` were the supersession guard, it would fail to distinguish them
    and A's token would be accepted onto B. It is NOT the guard; offer_id is. The
    trap for a future dev: do NOT remove the offer_id/HMAC mechanism believing
    `revision` protects supersession. It does not. This test's mutation proves it.

    MUTATION PROOF: neutralize the real guard by patching new_offer_id to a
    CONSTANT, so A and B share an offer_id. Now A's token (identical HMAC inputs)
    validates against B — a stale selection is accepted. That names the exact
    consequence of the offer_id guard's absence, and shows revision=1 on both did
    nothing to stop it.
    """
    async with pg_session_factory() as setup:
        await _seed(setup)
        await _seed_conversation(setup)
    start = _slot_start()

    from fonely.domain.booking.offers import OfferValidationError, serialize_offer

    # Offer A — the offer the caller was originally shown. Persist it; capture its
    # token. This is the token the caller will later submit to accept.
    offer_a = _build_offer(start)
    token_a = offer_a.slots[0].token  # type: ignore[attr-defined]
    serialized_a = serialize_offer(offer_a)

    # SUPERSEDE with a properly re-built offer B: identical slot params (same
    # start/end/expires_at, same resource/service) but a DIFFERENT offer_id, with
    # every slot token recomputed for that new offer_id — exactly what a fresh
    # build_offer produces when availability is re-derived. Rebuilding from A's
    # own params (rather than a second build_offer call) isolates the ONE variable
    # under test: only offer_id changes, so a rejection can only be attributed to
    # offer_id, not to an incidental expires_at drift between two now()s.
    superseding = _rebuild_with_offer_id(serialized_a, "supersededoffB")
    assert superseding["offer_id"] != serialized_a["offer_id"]
    assert superseding["revision"] == serialized_a["revision"] == 1, (
        "revision is vestigial: the superseding offer carries the SAME revision=1, "
        "so revision cannot distinguish it from the offer it replaced"
    )
    await _store_offer(pg_session_factory, superseding)

    # A's token, against the now-active B read back from JSONB, is refused — B's
    # tokens are bound to B's offer_id, so A's token is simply not in B.
    stored_b = await _read_active_offer(pg_session_factory)
    with pytest.raises(OfferValidationError) as exc:
        _validate_from_stored(stored_b, token_a)
    assert exc.value.code == "invalid_token", (
        f"stale token from a superseded offer must be refused as invalid_token, "
        f"got {exc.value.code!r}"
    )

    # MUTATION — remove the guard's ONE distinguishing variable: rebuild the
    # superseding offer with the SAME offer_id as A (freshness neutralized).
    # Everything else is already identical, so A's token now matches B's slot
    # token → the stale selection is ACCEPTED. This names the exact consequence of
    # losing offer_id freshness, AND — because revision is still 1 on both — proves
    # revision did nothing to stop the stale acceptance. offer_id is the guard.
    not_fresh = _rebuild_with_offer_id(serialized_a, str(serialized_a["offer_id"]))
    assert not_fresh["revision"] == 1
    await _store_offer(pg_session_factory, not_fresh)
    stored_not_fresh = await _read_active_offer(pg_session_factory)
    selected = _validate_from_stored(stored_not_fresh, token_a)  # no raise — trap sprung
    assert selected.token == token_a, (  # type: ignore[attr-defined]
        "with offer_id freshness removed, a superseded offer's token validated "
        "against the replacement — exactly the stale acceptance the offer_id/HMAC "
        "guard prevents, and revision=1-on-both did nothing to stop it"
    )


async def test_half_written_offer_fails_closed_after_persistence(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIORITY 3 — PERSISTENCE-FAILURE: a partially-written offer must never let
    a later turn read a coherent-looking booking. Fail closed.

    Simulate a write that stored the offer but LOST one slot's token (a partial
    JSONB write, or a truncated serialization). After the round-trip, the durable
    accept path must refuse — deserialize_offer recomputes the token and the
    stored (now empty) token cannot match, so the offer is unusable. It must not
    silently 'find' the slot by time and book it.

    DIFFERENTIAL MUTATION PROOF: the SAME slot, SAME token, with the offer intact
    validates cleanly (the positive control). Corrupt exactly one persisted field
    — drop the slot token — and the identical selection is refused. The refusal is
    caused by the corruption, not by an unrelated defect: change one thing, the
    outcome flips from accept to fail-closed.
    """
    async with pg_session_factory() as setup:
        await _seed(setup)
        await _seed_conversation(setup)
    start = _slot_start()

    from fonely.domain.booking.offers import serialize_offer

    offer = _build_offer(start)
    token = offer.slots[0].token  # type: ignore[attr-defined]
    intact = serialize_offer(offer)

    # Positive control: intact offer round-trips and validates.
    await _store_offer(pg_session_factory, intact)
    stored_ok = await _read_active_offer(pg_session_factory)
    ok = _validate_from_stored(stored_ok, token)
    assert ok.token == token  # type: ignore[attr-defined]

    # Corrupt: drop the token from the persisted slot (a half-written record).
    import copy

    corrupted = copy.deepcopy(intact)
    slots = corrupted["slots"]
    assert isinstance(slots, list) and slots
    assert isinstance(slots[0], dict)
    slots[0]["token"] = ""  # the lost field
    await _store_offer(pg_session_factory, corrupted)

    from fonely.domain.booking.offers import OfferValidationError

    stored_bad = await _read_active_offer(pg_session_factory)
    with pytest.raises(OfferValidationError) as exc:
        _validate_from_stored(stored_bad, token)
    # A dropped token makes the stored token mismatch the recomputed one →
    # tampered_token; deserialize raises rather than returning a usable offer.
    assert exc.value.code == "tampered_token", (
        f"a half-written offer (dropped slot token) must fail closed, got "
        f"{exc.value.code!r} — a coherent-looking booking was read from partial state"
    )


async def test_per_field_hmac_tampering_refused_after_persistence(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIORITY 4 — PER-FIELD HMAC tampering, through the JSONB round-trip.

    The slot token binds SIX fields (contract.generate_token): offer_id,
    slot_start, slot_end, resource_id, service_id, expires_at. Flipping ANY one
    in the persisted record — while leaving the stored token untouched — must be
    caught: deserialize recomputes the token from the tampered field and the
    mismatch raises tampered_token. The unit tests cover a single field in-memory;
    this proves EACH of the six is bound AND that the check runs against what
    Postgres returns.

    Each case IS its own mutation proof: change exactly one bound field, the
    outcome flips from accept to tampered_token. The positive control (untampered)
    validates, so a green here cannot be a check that always raises.
    """
    async with pg_session_factory() as setup:
        await _seed(setup)
        await _seed_conversation(setup)
    start = _slot_start()

    from fonely.domain.booking.offers import OfferValidationError, serialize_offer

    offer = _build_offer(start)
    token = offer.slots[0].token  # type: ignore[attr-defined]
    intact = serialize_offer(offer)

    # Positive control.
    await _store_offer(pg_session_factory, intact)
    assert _validate_from_stored(await _read_active_offer(pg_session_factory), token)

    # Each mutation keeps the slot interval VALID (end > start) so the token
    # recompute — not the interval invariant — is what rejects. slot_start moves
    # EARLIER (start-15) so end (start+30) still follows it; slot_end moves LATER.
    earlier_start = (start - timedelta(minutes=15)).isoformat()
    later_end = (start + timedelta(minutes=90)).isoformat()
    new_expiry = (offer.expires_at + timedelta(minutes=1)).isoformat()  # type: ignore[attr-defined]

    import copy

    # (field-path, mutated value) for each of the six HMAC-bound inputs.
    mutations: list[tuple[str, object]] = [
        ("offer_id", "tampered_offer"),
        ("resource_id", 999),
        ("service_id", 999),
        ("slot_start", earlier_start),
        ("slot_end", later_end),
        ("expires_at", new_expiry),
    ]

    for field, value in mutations:
        corrupted = copy.deepcopy(intact)
        if field == "slot_start":
            corrupted["slots"][0]["start_at_utc"] = value  # type: ignore[index]
        elif field == "slot_end":
            corrupted["slots"][0]["end_at_utc"] = value  # type: ignore[index]
        else:
            corrupted[field] = value
        await _store_offer(pg_session_factory, corrupted)
        stored = await _read_active_offer(pg_session_factory)
        with pytest.raises(OfferValidationError) as exc:
            _validate_from_stored(stored, token)
        # Every HMAC-bound field, when flipped, must surface as a rejection —
        # tampered_token for the fields the token recompute covers, or a bound
        # violation (invalid_expiry/ttl_exceeded) for expires_at, which the
        # recompute AND the expiry invariants both guard. Never a clean accept.
        assert exc.value.code in {
            "tampered_token",
            "invalid_expiry",
            "ttl_exceeded",
        }, (
            f"tampering the HMAC-bound field {field!r} was NOT rejected "
            f"(got {exc.value.code!r}) — the token binding does not cover it"
        )


async def test_deserialize_bounds_ttl_and_slot_count_mutation_proven(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIORITY 5 — ttl_exceeded / too_many_slots bounds, mutation-proven.

    deserialize_offer enforces two structural bounds independent of the HMAC:
      * ttl_exceeded — created_at..expires_at wider than OFFER_TTL_MINUTES (+1
        min grace). A forged long-lived offer is refused even if every token is
        internally consistent for that expiry.
      * invalid_slots — more than 100 slots. build_offer caps at 100 with
        too_many_slots; deserialize independently rejects an over-long stored list.

    MUTATION PROOF: for TTL, the SAME offer with an in-bounds expiry deserializes
    to a usable offer (positive control); widen created_at→expires_at past the
    cap and it is refused. One field changes; accept flips to reject.
    """
    async with pg_session_factory() as setup:
        await _seed(setup)
        await _seed_conversation(setup)
    start = _slot_start()

    from fonely.domain.booking.offers import (
        OFFER_TTL_MINUTES,
        OfferValidationError,
        deserialize_offer,
        serialize_offer,
    )

    offer = _build_offer(start)
    intact = serialize_offer(offer)

    # Positive control: in-bounds TTL deserializes.
    await _store_offer(pg_session_factory, intact)
    assert deserialize_offer(await _read_active_offer(pg_session_factory)) is not None

    # TTL mutation: push expires_at far beyond the cap relative to created_at.
    import copy

    over_ttl = copy.deepcopy(intact)
    created = datetime.fromisoformat(str(over_ttl["created_at"]))
    over_ttl["expires_at"] = (created + timedelta(minutes=OFFER_TTL_MINUTES + 30)).isoformat()
    await _store_offer(pg_session_factory, over_ttl)
    with pytest.raises(OfferValidationError) as exc:
        deserialize_offer(await _read_active_offer(pg_session_factory))
    assert exc.value.code == "ttl_exceeded", (
        f"an over-TTL stored offer must be refused, got {exc.value.code!r}"
    )

    # Slot-count mutation: more than 100 stored slots is refused structurally.
    too_many = copy.deepcopy(intact)
    one_slot = too_many["slots"][0]  # type: ignore[index]
    too_many["slots"] = [copy.deepcopy(one_slot) for _ in range(101)]  # type: ignore[assignment]
    await _store_offer(pg_session_factory, too_many)
    with pytest.raises(OfferValidationError) as exc2:
        deserialize_offer(await _read_active_offer(pg_session_factory))
    assert exc2.value.code == "invalid_slots", (
        f"an over-100-slot stored offer must be refused, got {exc2.value.code!r}"
    )
