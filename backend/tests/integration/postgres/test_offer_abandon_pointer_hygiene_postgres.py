r"""Abandon-path state hygiene for the durable offer selection pointers.

Companion to test_canonical_offer_hardening_postgres.py, which proved the ACCEPT
path (offer -> validate -> commit, concurrency + HMAC + supersede). This file
proves the ADJACENT ABANDON path leaves no orphan selection state — the same
"no orphan selection state" invariant enforced on the race-loss path in #4,
applied where an offer is invalidated instead of raced.

THE GAP THIS CLOSES (audit finding, then fix B):
_selected_token and _selected_offer_id are written by _try_offer_selection ONLY
after a successful selection (they also set start_at), and — grepped across the
whole src tree — are READ by no runtime code. Before the fix, no offer-abandon
site cleared them, so once a caller selected a slot and then abandoned it (named
a new date, negated the time, tripped an ambiguity bound, …), the pointers
OUTLIVED the offer they indexed and leaked into the PERSISTED conversations row
(collected_facts JSONB). Nothing read them at runtime, so nothing broke live —
but the durable snapshot showed a selection at a slot whose offer was gone, and
any future consumer that trusts them (resume-selection, audit/repair) would read
a lie. Fix B uses TWO narrow helpers, because there are two distinct abandon
events with different correct co-state:
  * _drop_active_offer(ctx) — the OFFER itself dies: clears the three inseparable
    keys (_active_offer + both pointers). Used at the 11 sites where a changed
    fact, a new date, an exhausted ambiguity bound, etc. invalidate the whole
    offer. Each site keeps its own additional site-specific pops.
  * _clear_selection_pointers(ctx) — the offer SURVIVES but the current SELECTION
    is rejected: clears ONLY the two pointers, KEEPS _active_offer. Used at the
    12th site — a bare "no" to a proposed slot at AWAITING_CONFIRMATION, where the
    caller may still pick a different slot from the same offer (reject-then-
    reselect). Dropping the offer here would break that flow, so it must not.

WRITES-ONLY EVIDENCE (the safety proof for clearing them): at fix time,
`grep -rn "_selected_token\|_selected_offer_id" src/` returns exactly the two
writes in conversation.py:_try_offer_selection and nothing else. Because nothing
reads them, clearing them on abandon cannot break a live flow — and the existing
booking-journey tests (which assert the pointers are PRESENT across the valid
selection->commit window) still pass, proving the fix does not over-clear the
window the pointers must survive.
"""

import uuid
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Import-origin assertion (venv discipline): exercise WORKTREE src, not the
# main-checkout src the shared .venv resolves without PYTHONPATH.
import fonely.services.conversation as _conv_mod
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.model_gateway import ModelResponse
from fonely.workers.inbound_worker import ClaimedEvent, _process_domain
from tests.integration.postgres.conftest import seed_whatsapp_channel

assert "/dev3-dental-e2e/" in _conv_mod.__file__, (
    f"conversation.py resolved from {_conv_mod.__file__!r} — not the worktree; "
    "run with PYTHONPATH=$PWD/src or the test exercises stale code"
)

pytestmark = pytest.mark.postgres

KOLKATA = ZoneInfo("Asia/Kolkata")

SENDER = "+919123456789"
_POINTERS = ("_selected_token", "_selected_offer_id")


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


async def _seed_clinic_narrow_hours(session: AsyncSession) -> None:
    """Clinic open 10:00-11:00 daily, 30-min slots: only 10:00 and 10:30 exist,
    so a 10:15 request is off-grid and yields an OFFER rather than a proposal."""
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
            "SELECT 1, day, '10:00', '11:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


def _claimed(event_id: int, body: str) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=event_id,
        business_id=1,
        message_id=f"wamid.abandon.{event_id}",
        sender_phone=SENDER,
        message_type="text",
        message_body=body,
        phone_number_id="phone-1",
        claim_token=uuid.uuid4(),
        claim_version=1,
        attempts=0,
        max_attempts=5,
    )


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure, let me help!")
    return gw


async def _drive(
    factory: async_sessionmaker[AsyncSession], gateway: AsyncMock, event_id: int, body: str
) -> None:
    async with factory() as session:
        await _process_domain(_claimed(event_id, body), session, gateway)
        await session.commit()


async def _select_a_slot(factory: async_sessionmaker[AsyncSession], gateway: AsyncMock) -> str:
    """Drive two natural turns to a real offer SELECTION and return the conv id.

    Turn 1: off-grid 10:15 request -> an _active_offer is stored, no pointers yet.
    Turn 2: patient names an offered time -> _try_offer_selection sets start_at,
    _selected_token, _selected_offer_id. This is the exact state whose pointers
    must later die on abandon.
    """
    await _drive(
        factory,
        gateway,
        1,
        "I want to book a general consultation with Dr. Priya tomorrow at "
        "10:15 am, reach me on +919123456789",
    )
    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" in ctx.collected_facts, (
        f"precondition: an offer must be active. Facts: {list(ctx.collected_facts)}"
    )
    assert "_selected_token" not in ctx.collected_facts, (
        "precondition: no selection pointer before the patient chooses"
    )

    await _drive(factory, gateway, 2, "let's do 10:30 am please")
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.collected_facts.get("_selected_token") is not None, (
        "precondition: the named slot must have gone through _try_offer_selection, "
        "setting the pointers we then expect abandon to clear"
    )
    assert ctx.collected_facts.get("_selected_offer_id") is not None
    return conv_id


async def _persisted_facts(factory: async_sessionmaker[AsyncSession], conv_id: str) -> dict:
    async with factory() as session:
        raw = await session.scalar(
            text("SELECT collected_facts FROM conversations WHERE id = :id"),
            {"id": conv_id},
        )
    assert isinstance(raw, dict), f"expected JSONB dict, got {type(raw)!r}"
    return raw


# The abandon that reaches a pointer-clearing pop site from a SELECTED state:
# after selection the conversation is AWAITING_CONFIRMATION, so the next message
# routes through _handle_confirmation. A "negative" decision that ALSO names a
# new date transitions to FACT_COLLECTION and re-runs _extract_datetime, whose
# "newly named time/date makes any active offer stale" branch calls
# _drop_active_offer. detect_confirmation must read this as negative (verified:
# "no, ... instead" is negative) AND parse_relative_date must find the date.
_ABANDON_NEW_DATE = "no, can we do the day after tomorrow instead"


async def test_new_date_after_selection_clears_pointers_in_memory_and_persisted(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIMARY: select a slot, then ABANDON by rejecting and naming a new date.
    The pointers must be gone BOTH in the live context AND in the persisted
    conversations row — the leak was specifically into durable persistence, so
    the row is the proof that matters.
    """
    async with pg_session_factory() as setup:
        await _seed_clinic_narrow_hours(setup)
    gateway = _mock_gateway()
    conv_id = await _select_a_slot(pg_session_factory, gateway)

    await _drive(pg_session_factory, gateway, 3, _ABANDON_NEW_DATE)

    ctx = _CONVERSATIONS[conv_id]
    # Precondition the abandon actually fired: the stale offer is gone.
    assert "_active_offer" not in ctx.collected_facts, (
        "the new-date rejection did not abandon the offer — test would prove "
        f"nothing about pointer hygiene. Facts: {list(ctx.collected_facts)}"
    )
    for key in _POINTERS:
        assert key not in ctx.collected_facts, (
            f"in-memory: {key} survived the offer abandon — a selection pointer "
            f"at a slot whose offer is gone. Facts: {list(ctx.collected_facts)}"
        )

    persisted = await _persisted_facts(pg_session_factory, conv_id)
    for key in _POINTERS:
        assert key not in persisted, (
            f"PERSISTED conversations.collected_facts still carries {key} after "
            f"abandon — the durable row shows a selection at a vanished offer. "
            f"Persisted keys: {list(persisted)}"
        )


async def test_skipping_the_pointer_clear_strands_state_mutation_proof(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANCHORED MUTATION PROOF: neutralize the fix — make _drop_active_offer clear
    ONLY _active_offer (the pre-fix bare pop) — and the exact same abandon strands
    the pointers. This proves the pointer-clear in the helper is load-bearing: it
    is the line that makes the persisted state honest. Without this mutation, the
    primary test could pass simply because the pointers were never set, or were
    cleared by some unrelated path.

    NOTE: _ABANDON_NEW_DATE is a NEGATIVE decision, so it traverses BOTH the
    negative branch's _clear_selection_pointers AND the _extract_datetime
    new-date _drop_active_offer. To isolate that the OFFER-DEATH clear is what
    would otherwise cover this path, both helpers are neutralized here — so if the
    pointer-clears were removed from BOTH, the pointers strand. With either real,
    they are cleared; the mutation removes both to expose the leak.
    """

    def _only_offer(ctx: object) -> None:
        # The pre-fix behaviour: drop the offer, leave the pointers dangling.
        ctx.collected_facts.pop("_active_offer", None)  # type: ignore[attr-defined]

    monkeypatch.setattr(ConversationService, "_drop_active_offer", staticmethod(_only_offer))
    monkeypatch.setattr(
        ConversationService, "_clear_selection_pointers", staticmethod(lambda ctx: None)
    )

    async with pg_session_factory() as setup:
        await _seed_clinic_narrow_hours(setup)
    gateway = _mock_gateway()
    conv_id = await _select_a_slot(pg_session_factory, gateway)

    await _drive(pg_session_factory, gateway, 3, _ABANDON_NEW_DATE)

    ctx = _CONVERSATIONS[conv_id]
    # The offer is gone (the abandon happened) but the pointers STRAND — this is
    # the dangling-pointer consequence the real helper prevents.
    assert "_active_offer" not in ctx.collected_facts, (
        "the abandon must still drop the offer even in the neutralized helper"
    )
    stranded_mem = [k for k in _POINTERS if k in ctx.collected_facts]
    persisted = await _persisted_facts(pg_session_factory, conv_id)
    stranded_persisted = [k for k in _POINTERS if k in persisted]
    assert stranded_mem and stranded_persisted, (
        "MUTATION DID NOT BITE: with the pointer-clear removed, the pointers were "
        "expected to strand (in memory and in the persisted row), proving the "
        "clear is load-bearing. They did not — so the primary tests would pass "
        f"for the wrong reason. mem={stranded_mem} persisted={stranded_persisted}"
    )


# ---------------------------------------------------------------------------
# The 12th abandon path: a BARE "no" to a proposed slot at AWAITING_CONFIRMATION.
# Unlike the 11 sites where the OFFER dies, here the offer legitimately SURVIVES
# (the caller may pick a different slot from the same offer) but the current
# SELECTION is rejected. So this site uses _clear_selection_pointers (drop the
# two pointers, KEEP _active_offer), not _drop_active_offer. These tests prove
# both halves: the stale pointers are cleared AND the offer is retained so
# reject-then-reselect still works — the guard against over-clearing.
# ---------------------------------------------------------------------------


async def _seed_clinic_wide_hours(session: AsyncSession) -> None:
    """Clinic open 10:00-12:00 daily, 30-min slots: a 10:15 off-grid request
    yields a MULTI-slot offer (10:00, 10:30, 11:00, 11:30), so a rejected slot
    still leaves OTHER slots in the same offer to reselect from."""
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
            "SELECT 1, day, '10:00', '12:00', true FROM generate_series(0, 6) AS day"
        )
    )
    await session.commit()


async def _select_10_30_from_multislot(
    factory: async_sessionmaker[AsyncSession], gateway: AsyncMock
) -> str:
    """Reach a SELECTION of 10:30 out of a multi-slot offer. Returns conv id.

    Asserts the offer carries MORE than one slot, so the later reject-then-
    reselect actually has another slot to move to (a single-slot offer could not
    distinguish 'reselect works' from 'offer happened to be dropped')."""
    await _drive(
        factory,
        gateway,
        1,
        "I want to book a general consultation with Dr. Priya tomorrow at "
        "10:15 am, reach me on +919123456789",
    )
    conv_id = next(iter(_CONVERSATIONS.keys()))
    ctx = _CONVERSATIONS[conv_id]
    offer = ctx.collected_facts.get("_active_offer")
    assert isinstance(offer, dict) and len(offer["slots"]) >= 2, (
        f"precondition: a multi-slot offer is required to prove reselection. "
        f"Got {offer['slots'] if isinstance(offer, dict) else offer!r}"
    )
    await _drive(factory, gateway, 2, "let's do 10:30 am please")
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.collected_facts.get("_selected_token") is not None
    assert ctx.state.value == "awaiting_confirmation", (
        f"precondition: selection should reach confirmation, got {ctx.state}"
    )
    return conv_id


async def test_bare_no_clears_pointers_but_keeps_offer_in_memory_and_persisted(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRIMARY (site 12): select a slot, then a BARE "no" (no replacement named),
    then a non-reselect turn. The rejected slot's pointers must be gone — in
    memory AND in the persisted row — while _active_offer is KEPT (the caller may
    still reselect). This is the leak the pointer-only clear closes.
    """
    async with pg_session_factory() as setup:
        await _seed_clinic_wide_hours(setup)
    gateway = _mock_gateway()
    conv_id = await _select_10_30_from_multislot(pg_session_factory, gateway)

    # Bare "no": reject THIS slot. Offer kept, pointers cleared.
    await _drive(pg_session_factory, gateway, 3, "no")

    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" in ctx.collected_facts, (
        "bare 'no' must KEEP the offer for reselection — dropping it would break "
        f"reject-then-pick-another. Facts: {list(ctx.collected_facts)}"
    )
    for key in _POINTERS:
        assert key not in ctx.collected_facts, (
            f"in-memory: {key} from the REJECTED slot survived a bare 'no' — it "
            f"points at a slot the caller refused. Facts: {list(ctx.collected_facts)}"
        )

    persisted = await _persisted_facts(pg_session_factory, conv_id)
    assert "_active_offer" in persisted, "persisted row must retain the kept offer"
    for key in _POINTERS:
        assert key not in persisted, (
            f"PERSISTED row still carries {key} after a bare 'no' — a durable "
            f"selection at a refused slot. Persisted keys: {list(persisted)}"
        )


async def test_bare_no_pointer_clear_is_load_bearing_mutation_proof(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANCHORED MUTATION (site 12): neutralize _clear_selection_pointers to a
    no-op. The same bare "no" then strands the rejected slot's pointers — proving
    the pointer-clear at the negative branch is load-bearing and that the primary
    test is not green for some unrelated reason.
    """
    monkeypatch.setattr(
        ConversationService, "_clear_selection_pointers", staticmethod(lambda ctx: None)
    )

    async with pg_session_factory() as setup:
        await _seed_clinic_wide_hours(setup)
    gateway = _mock_gateway()
    conv_id = await _select_10_30_from_multislot(pg_session_factory, gateway)

    await _drive(pg_session_factory, gateway, 3, "no")

    ctx = _CONVERSATIONS[conv_id]
    stranded_mem = [k for k in _POINTERS if k in ctx.collected_facts]
    persisted = await _persisted_facts(pg_session_factory, conv_id)
    stranded_persisted = [k for k in _POINTERS if k in persisted]
    assert stranded_mem and stranded_persisted, (
        "MUTATION DID NOT BITE: with _clear_selection_pointers a no-op, the "
        "rejected slot's pointers were expected to strand (memory + persisted). "
        f"They did not. mem={stranded_mem} persisted={stranded_persisted}"
    )


async def test_reject_then_reselect_still_works_no_overclear(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GUARD against over-clearing (the bug a full offer-drop at this site would
    cause): after a bare "no", the caller picks a DIFFERENT slot of the SAME
    offer by ordinal. Because the pointer-only clear KEEPS _active_offer, the
    reselection must succeed — a fresh proposal, back at AWAITING_CONFIRMATION,
    with new pointers. If this site had dropped the whole offer, this would break.
    """
    async with pg_session_factory() as setup:
        await _seed_clinic_wide_hours(setup)
    gateway = _mock_gateway()
    conv_id = await _select_10_30_from_multislot(pg_session_factory, gateway)

    await _drive(pg_session_factory, gateway, 3, "no")
    ctx = _CONVERSATIONS[conv_id]
    assert "_active_offer" in ctx.collected_facts, "offer must survive the 'no'"
    assert ctx.collected_facts.get("_selected_token") is None, (
        "the rejected slot's pointer must be cleared before reselection"
    )

    # Reselect a different slot from the SAME kept offer, by ordinal.
    await _drive(pg_session_factory, gateway, 4, "the first one")
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.state.value == "awaiting_confirmation", (
        f"reselection must re-propose and reach confirmation, got {ctx.state} — "
        "if the offer had been dropped, 'the first one' would match nothing"
    )
    assert ctx.proposal_id is not None, "reselection must create a fresh proposal"
    assert ctx.collected_facts.get("_selected_token") is not None, (
        "reselection must set a NEW selection pointer via _try_offer_selection"
    )
    assert "_active_offer" in ctx.collected_facts


async def test_valid_selection_window_keeps_pointers_never_cleared(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BELT: _clear_selection_pointers fires ONLY on rejection, never in the
    valid selection->commit window. A straight select-then-confirm (no "no")
    must carry the pointers all the way to a committed appointment — proving the
    new clear did not leak into the happy path.
    """
    async with pg_session_factory() as setup:
        await _seed_clinic_wide_hours(setup)
    gateway = _mock_gateway()
    conv_id = await _select_10_30_from_multislot(pg_session_factory, gateway)

    # Pointers present across the survive-window (set at 983-984, not cleared).
    ctx = _CONVERSATIONS[conv_id]
    assert ctx.collected_facts.get("_selected_token") is not None
    assert ctx.collected_facts.get("_selected_offer_id") is not None

    # Confirm -> commit. The happy path must not have been disturbed.
    await _drive(pg_session_factory, gateway, 3, "yes confirm")

    async with pg_session_factory() as verify:
        count = await verify.scalar(
            text("SELECT count(*) FROM appointments WHERE business_id = 1 AND status = 'confirmed'")
        )
    assert count == 1, (
        f"select-then-confirm must still book exactly one appointment, got {count} "
        "— the pointer-clear must not touch the valid selection window"
    )
