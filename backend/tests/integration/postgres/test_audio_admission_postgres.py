"""PostgreSQL tests for tenant-bound audio admission (migration 0017).

Two defects are closed here and neither is observable against a mock.

The first is that which clinic a dialled number reached lived in
EXOTEL_NUMBER_MAPPINGS, so attaching a number was a redeploy and nothing
stopped the same number appearing under two tenants. That is now a row with
constraints, and the constraints are what these tests exercise.

The second is that the audio-stream socket had no tenant at all. Admission
resolves one from a calls row our own ringing webhook wrote, keyed by the
provider's call id. The property worth proving is negative: a call id we never
observed must not reach any clinic, and an observed one must reach exactly the
clinic that was dialled and no other.

Every assertion here is row-level. A transcript-level check would pass on a
handler that logged the right thing and wrote the wrong row.
"""

from contextlib import contextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.core.config import settings
from fonely.repositories.channel_identities import (
    PROVIDER_EXOTEL,
    ChannelIdentityConflictError,
    ChannelIdentityRepository,
    register_channel_identity,
)
from fonely.services.audio_admission import AdmissionRefusal, admit_audio_stream

pytestmark = pytest.mark.postgres

_WEBHOOK_SECRET = "exotel-integration-secret"
_INTERNAL_SECRET = "internal-integration-secret"
_STATUS_ROUTE = "/webhooks/exotel/call-status"
_IDENTITY_ROUTE = "/internal/v1/businesses/channel-identity"


async def _seed_business(
    session: AsyncSession,
    business_id: int,
    name: str,
    timezone: str = "Asia/Kolkata",
) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:bid, :name, 'clinic', :phone, :tz, 'trial')"
        ),
        {
            "bid": business_id,
            "name": name,
            "phone": f"+91442835{business_id:04d}",
            "tz": timezone,
        },
    )


async def _insert_identity(
    session: AsyncSession,
    *,
    business_id: int,
    external_identifier: str,
    provider: str = PROVIDER_EXOTEL,
    status: str = "active",
    is_primary: bool = True,
) -> None:
    await session.execute(
        text(
            "INSERT INTO business_channel_identities "
            "(business_id, provider, external_identifier, status, is_primary) "
            "VALUES (:bid, :provider, :ident, :status, :primary)"
        ),
        {
            "bid": business_id,
            "provider": provider,
            "ident": external_identifier,
            "status": status,
            "primary": is_primary,
        },
    )
    await session.flush()


async def _observe_ringing(
    session: AsyncSession,
    *,
    business_id: int,
    call_sid: str,
    caller_phone: str = "+919876543210",
    provider: str = PROVIDER_EXOTEL,
) -> int:
    result = await session.execute(
        text(
            "INSERT INTO calls "
            "(business_id, caller_phone, call_provider, provider_call_sid, started_at) "
            "VALUES (:bid, :phone, :provider, :sid, NOW()) RETURNING id"
        ),
        {
            "bid": business_id,
            "phone": caller_phone,
            "provider": provider,
            "sid": call_sid,
        },
    )
    await session.flush()
    return int(result.scalar_one())


class TestChannelIdentityConstraints:
    """The database, not the application, is what makes these true."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_a_number_cannot_belong_to_two_tenants(self, pg_session: AsyncSession) -> None:
        """The defect that made env config unsafe, now unrepresentable.

        Nothing stopped two clinics claiming one number when this lived in a
        JSON blob, and the loser was whichever dict key was parsed second.
        """
        await _seed_business(pg_session, 1, "Smile Dental")
        await _seed_business(pg_session, 2, "Bright Dental")
        await _insert_identity(pg_session, business_id=1, external_identifier="+918000000001")

        with pytest.raises(IntegrityError):
            await _insert_identity(pg_session, business_id=2, external_identifier="+918000000001")

    @pytest.mark.asyncio(loop_scope="session")
    async def test_same_digits_under_another_provider_is_a_different_identity(
        self, pg_session: AsyncSession
    ) -> None:
        """Uniqueness is per provider, or a WhatsApp number would block a landline."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_identity(pg_session, business_id=1, external_identifier="+918000000001")
        await _insert_identity(
            pg_session,
            business_id=1,
            external_identifier="+918000000001",
            provider="some_other_provider",
        )

        repo = ChannelIdentityRepository(pg_session)
        assert await repo.resolve_business_id(PROVIDER_EXOTEL, "+918000000001") == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_only_one_active_primary_per_business_and_provider(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_identity(pg_session, business_id=1, external_identifier="+918000000001")

        with pytest.raises(IntegrityError):
            await _insert_identity(pg_session, business_id=1, external_identifier="+918000000002")

    @pytest.mark.asyncio(loop_scope="session")
    async def test_disabled_identity_resolves_to_nothing(self, pg_session: AsyncSession) -> None:
        """A decommissioned number stops writing into the clinic's records."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_identity(
            pg_session,
            business_id=1,
            external_identifier="+918000000001",
            status="disabled",
            is_primary=False,
        )

        repo = ChannelIdentityRepository(pg_session)
        assert await repo.resolve_business_id(PROVIDER_EXOTEL, "+918000000001") is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_registration_refuses_cross_tenant_capture(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session, 1, "Smile Dental")
        await _seed_business(pg_session, 2, "Bright Dental")
        await register_channel_identity(
            pg_session,
            business_id=1,
            provider=PROVIDER_EXOTEL,
            external_identifier="+918000000001",
        )

        with pytest.raises(ChannelIdentityConflictError):
            await register_channel_identity(
                pg_session,
                business_id=2,
                provider=PROVIDER_EXOTEL,
                external_identifier="+918000000001",
            )

        repo = ChannelIdentityRepository(pg_session)
        assert await repo.resolve_business_id(PROVIDER_EXOTEL, "+918000000001") == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_registering_a_second_number_moves_primary_rather_than_aborting(
        self, pg_session: AsyncSession
    ) -> None:
        """Demote-then-promote, or the partial unique index aborts the transaction."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await register_channel_identity(
            pg_session,
            business_id=1,
            provider=PROVIDER_EXOTEL,
            external_identifier="+918000000001",
        )
        await register_channel_identity(
            pg_session,
            business_id=1,
            provider=PROVIDER_EXOTEL,
            external_identifier="+918000000002",
        )

        repo = ChannelIdentityRepository(pg_session)
        assert await repo.resolve_identifier(1, PROVIDER_EXOTEL) == "+918000000002"
        # The old number still routes inbound calls. Demotion changes which
        # number we call out on, not which calls we accept.
        assert await repo.resolve_business_id(PROVIDER_EXOTEL, "+918000000001") == 1


class TestCallCorrelationConstraints:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_provider_and_sid_must_be_set_together(self, pg_session: AsyncSession) -> None:
        """Guards the NULL-distinct hole in the partial unique index.

        Postgres treats NULLs as distinct, so a unique index over
        (call_provider, provider_call_sid) would happily accept any number of
        rows with a NULL provider and the same sid. The paired CHECK makes
        that state unreachable instead of relying on callers to be careful.
        """
        await _seed_business(pg_session, 1, "Smile Dental")

        with pytest.raises(IntegrityError):
            await pg_session.execute(
                text(
                    "INSERT INTO calls (business_id, caller_phone, provider_call_sid, "
                    " started_at) VALUES (1, '+919876543210', 'orphan-sid', NOW())"
                )
            )

    @pytest.mark.asyncio(loop_scope="session")
    async def test_a_provider_call_id_cannot_be_recorded_twice(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session, 1, "Smile Dental")
        await _observe_ringing(pg_session, business_id=1, call_sid="sid-dup")

        with pytest.raises(IntegrityError):
            await _observe_ringing(pg_session, business_id=1, call_sid="sid-dup")

    @pytest.mark.asyncio(loop_scope="session")
    async def test_legacy_calls_without_a_provider_are_still_allowed(
        self, pg_session: AsyncSession
    ) -> None:
        """The columns are nullable on purpose: 0017 must not invalidate history."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await pg_session.execute(
            text(
                "INSERT INTO calls (business_id, caller_phone, started_at) "
                "VALUES (1, '+919876543210', NOW()), (1, '+919876543211', NOW())"
            )
        )
        await pg_session.flush()

        count = await pg_session.execute(
            text("SELECT count(*) FROM calls WHERE provider_call_sid IS NULL")
        )
        assert count.scalar_one() == 2


class TestAdmission:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_observed_call_admits_with_server_resolved_context(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session, 1, "Smile Dental", timezone="Asia/Kolkata")
        call_id = await _observe_ringing(pg_session, business_id=1, call_sid="sid-ok")

        result = await admit_audio_stream(
            pg_session, provider=PROVIDER_EXOTEL, provider_call_sid="sid-ok"
        )

        assert result.admitted
        assert result.session is not None
        assert result.session.business_id == 1
        assert result.session.call_id == call_id
        # Carried so the DPDP notice names the right clinic and the dialogue
        # grounds dates in the right timezone, from the same resolution that
        # bound the tenant rather than a second lookup that could disagree.
        assert result.session.clinic_name == "Smile Dental"
        assert result.session.timezone == "Asia/Kolkata"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unobserved_call_id_is_refused(self, pg_session: AsyncSession) -> None:
        """A forged or guessed id reaches no clinic at all."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _observe_ringing(pg_session, business_id=1, call_sid="sid-real")

        result = await admit_audio_stream(
            pg_session, provider=PROVIDER_EXOTEL, provider_call_sid="sid-forged"
        )

        assert not result.admitted
        assert result.refusal is AdmissionRefusal.UNOBSERVED_CALL

    @pytest.mark.asyncio(loop_scope="session")
    async def test_empty_call_id_is_refused_distinctly(self, pg_session: AsyncSession) -> None:
        """Absence must not read as success: no identifier is its own outcome."""
        result = await admit_audio_stream(
            pg_session, provider=PROVIDER_EXOTEL, provider_call_sid=""
        )

        assert not result.admitted
        assert result.refusal is AdmissionRefusal.NO_CALL_IDENTIFIER

    @pytest.mark.asyncio(loop_scope="session")
    async def test_ended_call_cannot_be_reopened(self, pg_session: AsyncSession) -> None:
        await _seed_business(pg_session, 1, "Smile Dental")
        await _observe_ringing(pg_session, business_id=1, call_sid="sid-ended")
        await pg_session.execute(
            text("UPDATE calls SET ended_at = NOW() WHERE provider_call_sid = 'sid-ended'")
        )
        await pg_session.flush()

        result = await admit_audio_stream(
            pg_session, provider=PROVIDER_EXOTEL, provider_call_sid="sid-ended"
        )

        assert not result.admitted
        assert result.refusal is AdmissionRefusal.CALL_ALREADY_ENDED

    @pytest.mark.asyncio(loop_scope="session")
    async def test_a_call_id_admits_only_its_own_tenant(self, pg_session: AsyncSession) -> None:
        """The isolation property, stated directly.

        Two clinics, two live calls. Each id resolves to the clinic that was
        actually dialled — there is no input to admission that would let one
        clinic's stream carry the other clinic's tenant.
        """
        await _seed_business(pg_session, 1, "Smile Dental")
        await _seed_business(pg_session, 2, "Bright Dental")
        await _observe_ringing(pg_session, business_id=1, call_sid="sid-clinic-1")
        await _observe_ringing(pg_session, business_id=2, call_sid="sid-clinic-2")

        first = await admit_audio_stream(
            pg_session, provider=PROVIDER_EXOTEL, provider_call_sid="sid-clinic-1"
        )
        second = await admit_audio_stream(
            pg_session, provider=PROVIDER_EXOTEL, provider_call_sid="sid-clinic-2"
        )

        assert first.session is not None and first.session.business_id == 1
        assert second.session is not None and second.session.business_id == 2

    @pytest.mark.asyncio(loop_scope="session")
    async def test_the_same_id_under_another_provider_does_not_admit(
        self, pg_session: AsyncSession
    ) -> None:
        await _seed_business(pg_session, 1, "Smile Dental")
        await _observe_ringing(pg_session, business_id=1, call_sid="sid-shared")

        result = await admit_audio_stream(
            pg_session, provider="some_other_provider", provider_call_sid="sid-shared"
        )

        assert not result.admitted
        assert result.refusal is AdmissionRefusal.UNOBSERVED_CALL


@contextmanager
def _secrets():
    original_webhook = settings.exotel_webhook_secret
    original_internal = settings.internal_api_secret
    object.__setattr__(settings, "exotel_webhook_secret", _WEBHOOK_SECRET)
    object.__setattr__(settings, "internal_api_secret", _INTERNAL_SECRET)
    try:
        yield
    finally:
        object.__setattr__(settings, "exotel_webhook_secret", original_webhook)
        object.__setattr__(settings, "internal_api_secret", original_internal)


def _webhook_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_WEBHOOK_SECRET}"}


def _internal_headers(business_id: int) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_INTERNAL_SECRET}",
        "X-Business-ID": str(business_id),
    }


def _app(pg_session_factory: async_sessionmaker[AsyncSession]):
    from fonely.app import create_app

    app = create_app()
    app.state.session_factory = pg_session_factory
    return app


class TestStatusWebhookAgainstRealRows:
    """The webhook is what makes a call observable, so its rows are the proof."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_retried_ringing_does_not_create_a_second_call(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Providers retry. A retry must not fork the call into two rows.

        Two rows would leave admission choosing between them, and the losing
        row would never be closed by the completion callback.
        """
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await _insert_identity(session, business_id=1, external_identifier="+918000000001")
            await session.commit()

        payload = {
            "CallSid": "retry-sid",
            "Status": "ringing",
            "To": "+918000000001",
            "From": "+919876543210",
        }
        with _secrets():
            transport = ASGITransport(app=_app(pg_session_factory))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post(_STATUS_ROUTE, json=payload, headers=_webhook_headers())
                second = await client.post(_STATUS_ROUTE, json=payload, headers=_webhook_headers())

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        async with pg_session_factory() as session:
            rows = await session.execute(
                text("SELECT id FROM calls WHERE provider_call_sid = 'retry-sid'")
            )
            assert len(rows.all()) == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_completion_closes_only_the_leg_it_names(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The redial case that "newest open call from this number" got wrong.

        One patient, two legs open at once. Completing the first must leave
        the second running, or a live conversation is recorded as finished.
        """
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await _insert_identity(session, business_id=1, external_identifier="+918000000001")
            await _observe_ringing(session, business_id=1, call_sid="leg-one")
            await _observe_ringing(session, business_id=1, call_sid="leg-two")
            await session.commit()

        with _secrets():
            transport = ASGITransport(app=_app(pg_session_factory))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _STATUS_ROUTE,
                    json={
                        "CallSid": "leg-one",
                        "Status": "completed",
                        "To": "+918000000001",
                        "From": "+919876543210",
                        "Duration": "120",
                    },
                    headers=_webhook_headers(),
                )

        assert response.status_code == 200, response.text
        async with pg_session_factory() as session:
            rows = await session.execute(
                text(
                    "SELECT provider_call_sid, ended_at, duration_sec FROM calls "
                    "WHERE business_id = 1 ORDER BY provider_call_sid"
                )
            )
            state = {sid: (ended, dur) for sid, ended, dur in rows.all()}

        assert state["leg-one"][0] is not None
        assert state["leg-one"][1] == 120
        assert state["leg-two"][0] is None, "the second leg was still live"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unregistered_number_writes_no_call_and_admits_nothing(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Fail-closed end to end: no tenant, no row, no stream."""
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await session.commit()

        with _secrets():
            transport = ASGITransport(app=_app(pg_session_factory))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _STATUS_ROUTE,
                    json={
                        "CallSid": "unmapped-sid",
                        "Status": "ringing",
                        "To": "+919999999999",
                        "From": "+919876543210",
                    },
                    headers=_webhook_headers(),
                )

        assert response.status_code == 404
        async with pg_session_factory() as session:
            count = await session.execute(text("SELECT count(*) FROM calls"))
            assert count.scalar_one() == 0
            admission = await admit_audio_stream(
                session, provider=PROVIDER_EXOTEL, provider_call_sid="unmapped-sid"
            )
        assert admission.refusal is AdmissionRefusal.UNOBSERVED_CALL


class TestRegistrationRoute:
    """Attaching a clinic's number is an API call, not a redeploy."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_registered_number_becomes_callable(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await session.commit()

        with _secrets():
            transport = ASGITransport(app=_app(pg_session_factory))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    _IDENTITY_ROUTE,
                    json={
                        "provider": PROVIDER_EXOTEL,
                        "external_identifier": "+918000000001",
                    },
                    headers=_internal_headers(1),
                )
                ringing = await client.post(
                    _STATUS_ROUTE,
                    json={
                        "CallSid": "post-registration-sid",
                        "Status": "ringing",
                        "To": "+918000000001",
                        "From": "+919876543210",
                    },
                    headers=_webhook_headers(),
                )

        assert registered.status_code == 201, registered.text
        assert ringing.status_code == 200, ringing.text
        async with pg_session_factory() as session:
            admission = await admit_audio_stream(
                session,
                provider=PROVIDER_EXOTEL,
                provider_call_sid="post-registration-sid",
            )
        assert admission.session is not None
        assert admission.session.business_id == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_route_refuses_another_tenants_number(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Re-pointing a live line is an operator decision, never a side effect."""
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await _seed_business(session, 2, "Bright Dental")
            await _insert_identity(session, business_id=1, external_identifier="+918000000001")
            await session.commit()

        with _secrets():
            transport = ASGITransport(app=_app(pg_session_factory))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _IDENTITY_ROUTE,
                    json={
                        "provider": PROVIDER_EXOTEL,
                        "external_identifier": "+918000000001",
                    },
                    headers=_internal_headers(2),
                )

        assert response.status_code == 409, response.text
        async with pg_session_factory() as session:
            repo = ChannelIdentityRepository(session)
            assert await repo.resolve_business_id(PROVIDER_EXOTEL, "+918000000001") == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_tenant_comes_from_the_header_not_the_body(
        self, pg_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A business_id in the body must not be able to register for someone else.

        The route rejects it outright rather than ignoring it. Ignoring would
        also be safe, but silently discarding a field the caller believed was
        authoritative is how an operator ends up convinced they registered a
        number for a tenant they did not.
        """
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await _seed_business(session, 2, "Bright Dental")
            await session.commit()

        with _secrets():
            transport = ASGITransport(app=_app(pg_session_factory))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _IDENTITY_ROUTE,
                    json={
                        "provider": PROVIDER_EXOTEL,
                        "external_identifier": "+918000000001",
                        "business_id": 2,
                    },
                    headers=_internal_headers(1),
                )

        assert response.status_code == 422, response.text
        async with pg_session_factory() as session:
            repo = ChannelIdentityRepository(session)
            # Neither tenant got it: the request was refused, not redirected.
            assert await repo.resolve_business_id(PROVIDER_EXOTEL, "+918000000001") is None
