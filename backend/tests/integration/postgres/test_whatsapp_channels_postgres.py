"""PostgreSQL tests for database-backed WhatsApp channel identity (0016).

These exercise the real constraints rather than a stand-in dictionary, because
the two properties that matter most — a provider number belongs to exactly one
tenant, and outbound selection is deterministic — are enforced by the database
and would not be observable against a mock.
"""

from contextlib import contextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.core.config import settings
from fonely.repositories.whatsapp_channels import (
    ChannelOwnershipConflictError,
    WhatsAppChannelRepository,
    register_channel,
)

pytestmark = pytest.mark.postgres


async def _seed_business(session: AsyncSession, business_id: int, name: str) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:bid, :name, 'clinic', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {"bid": business_id, "name": name, "phone": f"+91442835{business_id:04d}"},
    )


async def _insert_channel(
    session: AsyncSession,
    *,
    business_id: int,
    phone_number_id: str,
    status: str = "active",
    is_primary: bool = False,
) -> None:
    await session.execute(
        text(
            "INSERT INTO business_whatsapp_channels "
            "(business_id, phone_number_id, status, is_primary) "
            "VALUES (:bid, :pnid, :status, :primary)"
        ),
        {
            "bid": business_id,
            "pnid": phone_number_id,
            "status": status,
            "primary": is_primary,
        },
    )
    await session.flush()


class TestInboundTenantResolution:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_active_channel_resolves_to_its_owner(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="pnid-1")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("pnid-1") == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_unknown_number_resolves_to_none(self, pg_session: AsyncSession):
        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("never-registered") is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_disabled_channel_stops_accepting_traffic(self, pg_session: AsyncSession):
        """A decommissioned number must not keep writing into the clinic."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(
            pg_session, business_id=1, phone_number_id="pnid-old", status="disabled"
        )

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("pnid-old") is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_empty_number_resolves_to_none(self, pg_session: AsyncSession):
        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("") is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_two_tenants_cannot_share_one_number(self, pg_session: AsyncSession):
        """The isolation property: cross-tenant routing is unrepresentable."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _seed_business(pg_session, 2, "Bright Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="shared")

        with pytest.raises(IntegrityError):
            await _insert_channel(pg_session, business_id=2, phone_number_id="shared")


class TestOutboundSenderSelection:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_sole_active_channel_is_used(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="only")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) == "only"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_primary_wins_over_other_active_channels(self, pg_session: AsyncSession):
        """A multi-number clinic is the case the env var could not express."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="secondary")
        await _insert_channel(pg_session, business_id=1, phone_number_id="main", is_primary=True)

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) == "main"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_ambiguous_channels_refuse_rather_than_guess(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="a")
        await _insert_channel(pg_session, business_id=1, phone_number_id="b")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_disabled_channel_is_not_selected(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="off", status="disabled")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_business_without_channel_resolves_to_none(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_one_active_primary_per_business_is_enforced(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="p1", is_primary=True)

        with pytest.raises(IntegrityError):
            await _insert_channel(pg_session, business_id=1, phone_number_id="p2", is_primary=True)

    @pytest.mark.asyncio(loop_scope="session")
    async def test_disabled_primary_does_not_block_a_new_primary(self, pg_session: AsyncSession):
        """The uniqueness index is partial: retired channels must not wedge it."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(
            pg_session,
            business_id=1,
            phone_number_id="retired",
            status="disabled",
            is_primary=True,
        )
        await _insert_channel(pg_session, business_id=1, phone_number_id="current", is_primary=True)

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) == "current"


class TestOwnershipRecheck:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_owner_may_send_from_its_own_number(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="mine")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.owns_channel(1, "mine") is True

    @pytest.mark.asyncio(loop_scope="session")
    async def test_other_tenant_may_not_send_from_it(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _seed_business(pg_session, 2, "Bright Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="mine")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.owns_channel(2, "mine") is False

    @pytest.mark.asyncio(loop_scope="session")
    async def test_disabling_a_channel_revokes_send_rights(self, pg_session: AsyncSession):
        """Closes the enqueue-then-disable window."""
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="mine")
        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.owns_channel(1, "mine") is True

        await pg_session.execute(
            text(
                "UPDATE business_whatsapp_channels SET status = 'disabled' "
                "WHERE phone_number_id = 'mine'"
            )
        )
        await pg_session.flush()

        assert await repo.owns_channel(1, "mine") is False


class TestRegistration:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_register_makes_the_number_resolvable_both_ways(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await register_channel(
            pg_session, business_id=1, phone_number_id="new", display_phone_number="+914400"
        )

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("new") == 1
        assert await repo.resolve_phone_number_id(1) == "new"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_registering_a_second_number_demotes_the_first(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await register_channel(pg_session, business_id=1, phone_number_id="first")
        await register_channel(pg_session, business_id=1, phone_number_id="second")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_phone_number_id(1) == "second"
        # The old number keeps receiving; it is demoted, not disabled.
        assert await repo.resolve_business_id("first") == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_claiming_another_tenants_number_raises_and_writes_nothing(
        self, pg_session: AsyncSession
    ):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _seed_business(pg_session, 2, "Bright Dental")
        await register_channel(pg_session, business_id=1, phone_number_id="contested")

        with pytest.raises(ChannelOwnershipConflictError):
            await register_channel(pg_session, business_id=2, phone_number_id="contested")

        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("contested") == 1
        assert await repo.resolve_phone_number_id(2) is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_reregistering_the_same_number_is_idempotent(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await register_channel(pg_session, business_id=1, phone_number_id="same")
        await register_channel(
            pg_session, business_id=1, phone_number_id="same", display_phone_number="+9144"
        )

        count = await pg_session.scalar(
            text("SELECT COUNT(*) FROM business_whatsapp_channels WHERE business_id = 1")
        )
        assert count == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_reenabling_a_disabled_channel(self, pg_session: AsyncSession):
        await _seed_business(pg_session, 1, "Smile Dental")
        await _insert_channel(pg_session, business_id=1, phone_number_id="back", status="disabled")
        repo = WhatsAppChannelRepository(pg_session)
        assert await repo.resolve_business_id("back") is None

        await register_channel(pg_session, business_id=1, phone_number_id="back")
        assert await repo.resolve_business_id("back") == 1


_ROUTE = "/internal/v1/businesses/whatsapp-channel"
_SECRET = "test-secret-channels"


@contextmanager
def _internal_auth():
    original = settings.internal_api_secret
    object.__setattr__(settings, "internal_api_secret", _SECRET)
    try:
        yield
    finally:
        object.__setattr__(settings, "internal_api_secret", original)


def _headers(business_id: int) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_SECRET}",
        "X-Business-ID": str(business_id),
    }


class TestRegistrationRoute:
    """The supported path for making a clinic reachable.

    Without a mounted route the only way to attach a number is hand-written
    SQL, which is the operator-gated problem migration 0016 exists to remove.
    """

    async def _client(self):
        from fonely.app import create_app

        return create_app()

    @pytest.mark.asyncio(loop_scope="session")
    async def test_route_registers_and_resolves(
        self,
        pg_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await session.commit()

        with _internal_auth():
            app = await self._client()
            app.state.session_factory = pg_session_factory
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _ROUTE, json={"phone_number_id": "route-1"}, headers=_headers(1)
                )

        assert response.status_code == 201, response.text
        async with pg_session_factory() as session:
            assert await WhatsAppChannelRepository(session).resolve_business_id("route-1") == 1

    @pytest.mark.asyncio(loop_scope="session")
    async def test_route_refuses_another_tenants_number(
        self,
        pg_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Cross-tenant capture must be a conflict, never a silent reassignment."""
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await _seed_business(session, 2, "Bright Dental")
            await session.commit()

        with _internal_auth():
            app = await self._client()
            app.state.session_factory = pg_session_factory
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post(
                    _ROUTE, json={"phone_number_id": "contested"}, headers=_headers(1)
                )
                second = await client.post(
                    _ROUTE, json={"phone_number_id": "contested"}, headers=_headers(2)
                )

        assert first.status_code == 201, first.text
        assert second.status_code == 409, second.text
        async with pg_session_factory() as session:
            repo = WhatsAppChannelRepository(session)
            assert await repo.resolve_business_id("contested") == 1
            assert await repo.resolve_phone_number_id(2) is None

    @pytest.mark.asyncio(loop_scope="session")
    async def test_route_rejects_unauthenticated_caller(
        self,
        pg_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with pg_session_factory() as session:
            await _seed_business(session, 1, "Smile Dental")
            await session.commit()

        with _internal_auth():
            app = await self._client()
            app.state.session_factory = pg_session_factory
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _ROUTE,
                    json={"phone_number_id": "unauth"},
                    headers={"Authorization": "Bearer wrong", "X-Business-ID": "1"},
                )

        assert response.status_code == 401
        async with pg_session_factory() as session:
            assert await WhatsAppChannelRepository(session).resolve_business_id("unauth") is None
