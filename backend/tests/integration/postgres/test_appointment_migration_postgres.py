"""PostgreSQL contracts for appointment migration and database invariants.

These tests are collected everywhere and execute only against the guarded local
PostgreSQL test database configured by the integration-test fixtures.
"""

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).parents[3]


def _pg_constraint_name(error: IntegrityError) -> str | None:
    cause = error.orig
    while cause is not None:
        name = getattr(cause, "constraint_name", None)
        if name is not None:
            return name
        cause = getattr(cause, "__cause__", None)
    return None


START = datetime(2026, 8, 3, 10, tzinfo=UTC)


def _run_alembic(
    database_url: str,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.replace(database_url, "[REDACTED_DATABASE_URL]")
        pytest.fail(f"Alembic {' '.join(args)} failed:\n{stderr}")
    return result


def _appointment_payload(
    appointment_id: int,
    *,
    start_at: datetime,
    end_at: datetime,
    service_id: int = 1,
    resource_id: int = 1,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "appointment",
        "data": {
            "operation": "create",
            "customer_phone": "+919123456789",
            "call_id": None,
            "facts": {
                "service_id": service_id,
                "service_name": "Haircut",
                "resource_id": resource_id,
                "resource_name": "Priya",
                "duration_minutes": 30,
                "buffer_before_minutes": 0,
                "buffer_after_minutes": 0,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
                "effective_start_at": start_at.isoformat(),
                "effective_end_at": end_at.isoformat(),
                "business_timezone": "Asia/Kolkata",
                "price": "500.00",
            },
        },
    }


async def _seed_0003_catalog(connection: object) -> None:
    await connection.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Legacy Salon', 'salon', '+919000000001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO services (id, business_id, name, duration_minutes, price, is_active) "
            "VALUES (1, 1, 'Haircut', 30, 500.00, true)"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Priya', 'staff', true)"
        )
    )


async def _seed_0003_appointment(
    connection: object,
    appointment_id: int,
    *,
    start_at: datetime,
    status: str = "confirmed",
    with_provenance: bool = True,
) -> None:
    end_at = start_at + timedelta(minutes=30)
    pending_action_id = appointment_id if with_provenance else None
    if with_provenance:
        payload = _appointment_payload(appointment_id, start_at=start_at, end_at=end_at)
        await connection.execute(
            text(
                "INSERT INTO pending_actions "
                "(id, business_id, action_type, payload_schema_version, proposed_payload, "
                "payload_digest, status, expires_at, idempotency_key, committed_entity_type, "
                "committed_entity_id, version) VALUES "
                "(:id, 1, 'appointment', 1, CAST(:payload AS jsonb), :digest, 'confirmed', "
                ":expires_at, :key, 'appointment', :id, 1)"
            ),
            {
                "id": appointment_id,
                "payload": json.dumps(payload),
                "digest": f"{appointment_id:064x}",
                "expires_at": start_at + timedelta(hours=1),
                "key": f"legacy-action-{appointment_id}",
            },
        )
    await connection.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_phone, start_at, end_at, "
            "status, idempotency_key, pending_action_id, created_at) VALUES "
            "(:id, 1, 1, 1, '+919123456789', :start_at, :end_at, :status, :key, "
            ":pending_action_id, :created_at)"
        ),
        {
            "id": appointment_id,
            "start_at": start_at,
            "end_at": end_at,
            "status": status,
            "key": f"legacy-appointment-{appointment_id}",
            "pending_action_id": pending_action_id,
            "created_at": start_at - timedelta(days=1),
        },
    )


async def _prepare_0003(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    _run_alembic(postgres_database_url, "downgrade", "0003")
    async with pg_engine.begin() as connection:
        await _seed_0003_catalog(connection)


async def _restore_head_after_failed_preflight(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    async with pg_engine.begin() as connection:
        await connection.execute(text("DELETE FROM appointments"))
        await connection.execute(text("DELETE FROM pending_actions"))
    _run_alembic(postgres_database_url, "upgrade", "head")


async def test_populated_valid_0003_upgrades_with_exact_snapshots_and_allocation(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    await _prepare_0003(pg_engine, postgres_database_url)
    async with pg_engine.begin() as connection:
        await _seed_0003_appointment(connection, 1, start_at=START)

    _run_alembic(postgres_database_url, "upgrade", "0004")

    async with pg_engine.connect() as connection:
        appointment = (
            await connection.execute(
                text(
                    "SELECT service_name_snapshot, resource_name_snapshot, "
                    "duration_minutes_snapshot, buffer_before_minutes_snapshot, "
                    "buffer_after_minutes_snapshot, effective_start_at, effective_end_at, "
                    "source, version FROM appointments WHERE id = 1"
                )
            )
        ).one()
        allocation = (
            await connection.execute(
                text(
                    "SELECT business_id, resource_id, appointment_id, pending_action_id, "
                    "allocation_type, status, source, effective_start_at, effective_end_at, "
                    "idempotency_key, version FROM resource_allocations WHERE appointment_id = 1"
                )
            )
        ).one()

    assert appointment.service_name_snapshot == "Haircut"
    assert appointment.resource_name_snapshot == "Priya"
    assert appointment.duration_minutes_snapshot == 30
    assert appointment.buffer_before_minutes_snapshot == 0
    assert appointment.buffer_after_minutes_snapshot == 0
    assert appointment.effective_start_at == START
    assert appointment.effective_end_at == START + timedelta(minutes=30)
    assert appointment.source == "customer_conversation"
    assert appointment.version == 1
    assert allocation.business_id == 1
    assert allocation.resource_id == 1
    assert allocation.appointment_id == 1
    assert allocation.pending_action_id == 1
    assert allocation.allocation_type == "appointment"
    assert allocation.status == "active"
    assert allocation.source == "customer_conversation"
    assert allocation.effective_start_at == appointment.effective_start_at
    assert allocation.effective_end_at == appointment.effective_end_at
    assert allocation.idempotency_key == "legacy-appointment-1"
    assert allocation.version == 1

    async with pg_engine.connect() as connection:
        held_until_count = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'appointments' "
                "AND column_name = 'held_until'"
            )
        )
    assert held_until_count == 0


InvalidSeeder = Callable[[object], object]


async def _invalid_missing_provenance(connection: object) -> None:
    await _seed_0003_appointment(connection, 1, start_at=START, with_provenance=False)


async def _invalid_held_appointment(connection: object) -> None:
    await _seed_0003_appointment(connection, 1, start_at=START, status="held")


async def _invalid_overlapping_appointments(connection: object) -> None:
    await _seed_0003_appointment(connection, 1, start_at=START)
    await _seed_0003_appointment(connection, 2, start_at=START + timedelta(minutes=15))


async def _seed_invalid_provenance_value(
    connection: object,
    path: tuple[str, ...],
    value: object,
) -> None:
    await _seed_0003_appointment(connection, 1, start_at=START)
    payload = _appointment_payload(
        1,
        start_at=START,
        end_at=START + timedelta(minutes=30),
    )
    if path[-1] == "target_appointment_id":
        payload_data = payload["data"]
        assert isinstance(payload_data, dict)
        payload_data["operation"] = "cancel"
    target: dict[str, object] = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment]
    target[path[-1]] = value
    await connection.execute(
        text("UPDATE pending_actions SET proposed_payload = CAST(:payload AS jsonb) WHERE id = 1"),
        {"payload": json.dumps(payload)},
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("data", "facts", "service_id"), 10**100),
        (("data", "facts", "resource_id"), 10**100),
        (("data", "call_id"), 10**100),
        (("data", "facts", "duration_minutes"), 10**100),
        (("data", "facts", "buffer_before_minutes"), 10**100),
        (("data", "facts", "buffer_after_minutes"), 10**100),
        (("data", "target_appointment_id"), 10**100),
        (("schema_version",), 10**100),
        (("data", "facts", "price"), "1e1000000"),
        (("data", "facts", "start_at"), "not-a-timestamp+00:00"),
        (("data", "facts", "effective_end_at"), "2026-99-99T00:00:00+00:00"),
    ],
    ids=[
        "service-id-overflow",
        "resource-id-overflow",
        "call-id-overflow",
        "duration-overflow",
        "buffer-before-overflow",
        "buffer-after-overflow",
        "target-id-overflow",
        "schema-version-overflow",
        "numeric-overflow",
        "start-malformed",
        "effective-end-malformed",
    ],
)
async def test_oversized_or_malformed_provenance_is_sanitized_before_ddl(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
    path: tuple[str, ...],
    value: object,
) -> None:
    await _prepare_0003(pg_engine, postgres_database_url)
    try:
        async with pg_engine.begin() as connection:
            await _seed_invalid_provenance_value(connection, path, value)

        result = _run_alembic(postgres_database_url, "upgrade", "0004", check=False)
        assert result.returncode != 0
        assert "Migration 0004" in result.stderr
        assert "value out of range" not in result.stderr
        assert "invalid input syntax" not in result.stderr

        async with pg_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            ddl_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'services' "
                    "AND column_name = 'buffer_before_minutes'"
                )
            )
        assert revision == "0003"
        assert ddl_count == 0
    finally:
        await _restore_head_after_failed_preflight(pg_engine, postgres_database_url)


@pytest.mark.parametrize(
    ("seed_invalid", "expected_message"),
    [
        (_invalid_missing_provenance, "cannot determine appointment creation provenance"),
        (_invalid_held_appointment, "does not reinterpret legacy held appointments"),
        (_invalid_overlapping_appointments, "overlapping capacity-bearing legacy appointments"),
    ],
    ids=["missing-provenance", "held", "overlap"],
)
async def test_invalid_0003_preflight_is_atomic_before_ddl(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
    seed_invalid: InvalidSeeder,
    expected_message: str,
) -> None:
    await _prepare_0003(pg_engine, postgres_database_url)
    try:
        async with pg_engine.begin() as connection:
            await seed_invalid(connection)

        result = _run_alembic(postgres_database_url, "upgrade", "0004", check=False)
        assert result.returncode != 0
        assert expected_message in result.stderr

        async with pg_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            buffer_column = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'services' "
                    "AND column_name = 'buffer_before_minutes'"
                )
            )
        assert revision == "0003"
        assert buffer_column == 0
    finally:
        await _restore_head_after_failed_preflight(pg_engine, postgres_database_url)


async def _seed_head_catalog(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Salon', 'salon', '+919000000001', 'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'Haircut', 30, 0, 0, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Priya', 'staff', true)"
        )
    )


async def _insert_head_appointment(
    session: AsyncSession,
    appointment_id: int,
    *,
    start_at: datetime,
    status: str = "confirmed",
    version: int = 1,
) -> None:
    end_at = start_at + timedelta(minutes=30)
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_phone, start_at, end_at, "
            "effective_start_at, effective_end_at, service_name_snapshot, "
            "resource_name_snapshot, duration_minutes_snapshot, "
            "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
            "business_timezone_snapshot, status, source, "
            "idempotency_key, version, created_at, updated_at) VALUES "
            "(:id, 1, 1, 1, '+919123456789', :start_at, :end_at, :start_at, :end_at, "
            "'Haircut', 'Priya', 30, 0, 0, 'Asia/Kolkata', :status, "
            "'owner_manual', :key, :version, now(), now())"
        ),
        {
            "id": appointment_id,
            "start_at": start_at,
            "end_at": end_at,
            "status": status,
            "key": f"appointment-{appointment_id}",
            "version": version,
        },
    )


async def _insert_active_allocation(
    session: AsyncSession,
    appointment_id: int,
    *,
    start_at: datetime,
    resource_id: int = 1,
    end_at: datetime | None = None,
    source: str = "owner_manual",
    allocation_type: str = "manual_appointment",
) -> None:
    await session.execute(
        text(
            "INSERT INTO resource_allocations "
            "(business_id, resource_id, appointment_id, allocation_type, status, source, "
            "effective_start_at, effective_end_at, idempotency_key, version) VALUES "
            "(1, :resource_id, :appointment_id, :allocation_type, 'active', :source, "
            ":start_at, :end_at, :key, 1)"
        ),
        {
            "appointment_id": appointment_id,
            "resource_id": resource_id,
            "allocation_type": allocation_type,
            "source": source,
            "start_at": start_at,
            "end_at": end_at or start_at + timedelta(minutes=30),
            "key": f"allocation-{appointment_id}",
        },
    )


async def test_active_allocations_reject_overlap_and_allow_exact_adjacency(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await _insert_head_appointment(session, 2, start_at=START + timedelta(minutes=30))
        await _insert_active_allocation(session, 2, start_at=START + timedelta(minutes=30))
        await session.commit()

    async with pg_session_factory() as session:
        await _insert_head_appointment(session, 3, start_at=START + timedelta(minutes=15))
        with pytest.raises(IntegrityError) as error:
            await _insert_active_allocation(session, 3, start_at=START + timedelta(minutes=15))
        assert getattr(error.value.orig, "sqlstate", None) == "23P01"
        assert _pg_constraint_name(error.value) == "ex_resource_allocations_active_overlap"
        await session.rollback()


async def test_head_rejects_held_appointment_status(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        with pytest.raises(IntegrityError) as error:
            await _insert_head_appointment(session, 1, start_at=START, status="held")
        assert _pg_constraint_name(error.value) == "appointment_status"
        await session.rollback()


async def test_appointment_update_to_confirmed_requires_matching_allocation(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    past = datetime(2020, 1, 1, 10, tzinfo=UTC)
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=past, status="completed")
        await session.commit()

    async with pg_session_factory() as session:
        await session.execute(
            text("UPDATE appointments SET status = 'confirmed', version = version + 1 WHERE id = 1")
        )
        with pytest.raises(IntegrityError) as error:
            await session.commit()
        assert _pg_constraint_name(error.value) == ("ck_confirmed_appointment_active_allocation")


@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_allocation_update_and_delete_paths_preserve_confirmed_symmetry(
    pg_session_factory: async_sessionmaker[AsyncSession],
    mutation: str,
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await session.commit()

    async with pg_session_factory() as session:
        if mutation == "update":
            await session.execute(
                text(
                    "UPDATE resource_allocations SET status = 'released', "
                    "version = version + 1 WHERE appointment_id = 1"
                )
            )
        else:
            await session.execute(text("DELETE FROM resource_allocations WHERE appointment_id = 1"))
        with pytest.raises(IntegrityError) as error:
            await session.commit()
        expected_constraint = (
            "ck_confirmed_appointment_active_allocation"
            if mutation == "update"
            else "ck_resource_allocation_immutable_identity"
        )
        assert _pg_constraint_name(error.value) == expected_constraint


@pytest.mark.parametrize("update_order", ["appointment-first", "allocation-first"])
async def test_appointment_and_allocation_facts_update_in_either_order(
    pg_session_factory: async_sessionmaker[AsyncSession],
    update_order: str,
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await session.commit()

    async with pg_session_factory() as session:
        if update_order == "appointment-first":
            shifted = {
                "start_at": START + timedelta(hours=1),
                "end_at": START + timedelta(hours=1, minutes=30),
            }
            await session.execute(
                text(
                    "UPDATE appointments SET start_at = :start_at, end_at = :end_at, "
                    "effective_start_at = :start_at, effective_end_at = :end_at, "
                    "rescheduled_at = now(), version = version + 1 WHERE id = 1"
                ),
                shifted,
            )
            with pytest.raises(IntegrityError) as error:
                await session.execute(
                    text(
                        "UPDATE resource_allocations SET effective_start_at = :start_at, "
                        "effective_end_at = :end_at, version = version + 1 "
                        "WHERE appointment_id = 1"
                    ),
                    shifted,
                )
            assert _pg_constraint_name(error.value) == ("ck_resource_allocation_immutable_identity")
        else:
            with pytest.raises(IntegrityError) as error:
                await session.execute(
                    text(
                        "UPDATE resource_allocations SET effective_start_at = :start_at, "
                        "effective_end_at = :end_at, version = version + 1 "
                        "WHERE appointment_id = 1"
                    ),
                    {
                        "start_at": START + timedelta(hours=1),
                        "end_at": START + timedelta(hours=1, minutes=30),
                    },
                )
            assert _pg_constraint_name(error.value) == ("ck_resource_allocation_immutable_identity")
        await session.rollback()


@pytest.mark.parametrize("status", ["confirmed", "completed", "no_show"])
@pytest.mark.parametrize(
    ("mismatch", "kwargs"),
    [
        ("resource", {"resource_id": 2}),
        ("start", {"start_at_delta": timedelta(minutes=1)}),
        ("end", {"end_at_delta": timedelta(minutes=31)}),
        ("source", {"source": "walk_in", "allocation_type": "walk_in"}),
    ],
)
async def test_capacity_appointment_rejects_nonmatching_active_allocation(
    pg_session_factory: async_sessionmaker[AsyncSession],
    status: str,
    mismatch: str,
    kwargs: dict[str, object],
) -> None:
    del mismatch
    appointment_start = START if status == "confirmed" else datetime(2020, 1, 1, 10, tzinfo=UTC)
    allocation_start = appointment_start + kwargs.get("start_at_delta", timedelta())  # type: ignore[operator]
    allocation_end = appointment_start + kwargs.get("end_at_delta", timedelta(minutes=30))  # type: ignore[operator]
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        if kwargs.get("resource_id") == 2:
            await session.execute(
                text(
                    "INSERT INTO resources "
                    "(id, business_id, name, resource_type, is_active) "
                    "VALUES (2, 1, 'Mira', 'staff', true)"
                )
            )
        await _insert_head_appointment(session, 1, start_at=appointment_start, status=status)
        await _insert_active_allocation(
            session,
            1,
            start_at=allocation_start,
            resource_id=kwargs.get("resource_id", 1),  # type: ignore[arg-type]
            end_at=allocation_end,
            source=kwargs.get("source", "owner_manual"),  # type: ignore[arg-type]
            allocation_type=kwargs.get("allocation_type", "manual_appointment"),  # type: ignore[arg-type]
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert _pg_constraint_name(error.value) == ("ck_confirmed_appointment_active_allocation")
        await session.rollback()


@pytest.mark.parametrize("status", ["completed", "no_show"])
async def test_terminal_transition_retains_active_allocation(
    pg_session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    past = datetime(2020, 1, 1, 10, tzinfo=UTC)
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=past)
        await _insert_active_allocation(session, 1, start_at=past)
        await session.commit()

    async with pg_session_factory() as session:
        await session.execute(
            text("UPDATE appointments SET status = :status, version = version + 1 WHERE id = 1"),
            {"status": status},
        )
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await session.commit()


@pytest.mark.parametrize("status", ["completed", "no_show"])
async def test_terminal_transition_with_released_allocation_fails(
    pg_session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    past = datetime(2020, 1, 1, 10, tzinfo=UTC)
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=past)
        await _insert_active_allocation(session, 1, start_at=past)
        await session.commit()

    async with pg_session_factory() as session:
        await session.execute(
            text("UPDATE appointments SET status = :status, version = version + 1 WHERE id = 1"),
            {"status": status},
        )
        await session.execute(
            text(
                "UPDATE resource_allocations SET status = 'released', "
                "version = version + 1 WHERE appointment_id = 1"
            )
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert _pg_constraint_name(error.value) == ("ck_confirmed_appointment_active_allocation")
        await session.rollback()


@pytest.mark.parametrize("status", ["completed", "no_show"])
async def test_premature_terminal_transition_rejected(
    pg_session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await session.commit()

    async with pg_session_factory() as session:
        await session.execute(
            text("UPDATE appointments SET status = :status, version = version + 1 WHERE id = 1"),
            {"status": status},
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert "before effective interval ends" in str(error.value)
        await session.rollback()


async def test_appointment_insert_then_delete_is_rejected_at_deferred_boundary(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await session.execute(text("DELETE FROM appointments WHERE id = 1"))
        with pytest.raises(IntegrityError) as error:
            await session.commit()
        assert _pg_constraint_name(error.value) == ("ck_appointment_mutation_commit")


@pytest.mark.parametrize(
    ("mutation_sql", "expected_message"),
    [
        (
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id) VALUES (1, 1, 1)",
            "cannot preserve service-resource eligibility",
        ),
        (
            "UPDATE services SET buffer_before_minutes = 5 WHERE id = 1",
            "cannot preserve service buffers",
        ),
        (
            "INSERT INTO operating_schedules "
            "(business_id, resource_id, day_of_week, open_time, close_time, is_active) "
            "VALUES (1, 1, 1, '09:00', '17:00', true)",
            "cannot preserve resource-specific schedules",
        ),
        (
            "INSERT INTO schedule_exceptions "
            "(business_id, resource_id, exception_date, is_closed) "
            "VALUES (1, 1, '2026-08-04', true)",
            "cannot preserve resource-specific exceptions",
        ),
    ],
    ids=["eligibility", "service-buffer", "resource-schedule", "resource-exception"],
)
async def test_lossy_0004_downgrade_is_rejected_atomically(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
    mutation_sql: str,
    expected_message: str,
) -> None:
    await _prepare_0003(pg_engine, postgres_database_url)
    async with pg_engine.begin() as connection:
        await _seed_0003_appointment(connection, 1, start_at=START)
    _run_alembic(postgres_database_url, "upgrade", "0004")
    try:
        async with pg_engine.begin() as connection:
            await connection.execute(text(mutation_sql))
        result = _run_alembic(postgres_database_url, "downgrade", "0003", check=False)
        assert result.returncode != 0
        assert expected_message in result.stderr
        async with pg_engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            allocation_table = await connection.scalar(
                text("SELECT to_regclass('resource_allocations') IS NOT NULL")
            )
        assert revision == "0004"
        assert allocation_table is True
    finally:
        # Leave the database on head before fixture truncation even if an assertion
        # fails after Alembic moved unexpectedly.
        _run_alembic(postgres_database_url, "upgrade", "head", check=False)
        async with pg_engine.begin() as connection:
            await connection.execute(text("DELETE FROM service_resource_eligibility"))
            await connection.execute(text("DELETE FROM operating_schedules"))
            await connection.execute(text("DELETE FROM schedule_exceptions"))
            await connection.execute(
                text("UPDATE services SET buffer_before_minutes = 0 WHERE id = 1")
            )


@pytest.mark.parametrize(
    ("payload", "expected_call_id", "expected_result"),
    [
        (None, None, False),
        (None, 1, False),
        ("[]", None, False),
        ("{}", None, False),
        ('{"call_id": null}', None, True),
        ('{"call_id": null}', 1, False),
        ('{"call_id": 1}', 1, True),
        ('{"call_id": 2147483647}', 2147483647, True),
        ('{"call_id": 7}', 8, False),
        ('{"call_id": "7"}', 7, False),
        ('{"call_id": 0}', None, False),
        ('{"call_id": -1}', None, False),
        ('{"call_id": 1.0}', 1, False),
        ('{"call_id": 2147483648}', None, False),
        ('{"call_id": 10000000000000000000000000000000000000000}', None, False),
        ('{"call_id": -10000000000000000000000000000000000000000}', None, False),
        ('{"call_id": true}', None, False),
        ('{"call_id": {}}', None, False),
        ('{"call_id": []}', None, False),
    ],
    ids=[
        "sql-null-null",
        "sql-null-value",
        "non-object",
        "missing",
        "json-null-null",
        "json-null-value",
        "one",
        "maximum",
        "mismatch",
        "numeric-string",
        "zero",
        "negative",
        "decimal",
        "above-maximum",
        "positive-overflow",
        "negative-overflow",
        "boolean",
        "object",
        "array",
    ],
)
async def test_appointment_payload_call_id_matcher_is_total(
    pg_engine: AsyncEngine,
    payload: str | None,
    expected_call_id: int | None,
    expected_result: bool,
) -> None:
    async with pg_engine.connect() as connection:
        result = await connection.scalar(
            text(
                "SELECT appointment_payload_call_id_matches("
                "CAST(:payload AS jsonb), CAST(:expected_call_id AS bigint))"
            ),
            {"payload": payload, "expected_call_id": expected_call_id},
        )
    assert result is expected_result
    assert result is not None


@pytest.mark.parametrize(
    "field_name",
    ["target_appointment_id", "target_expected_version"],
)
@pytest.mark.parametrize(
    ("payload", "expected_value", "expected_result"),
    [
        (None, 1, False),
        ("[]", 1, False),
        ("{}", 1, False),
        ('{"other": 1}', 1, False),
        ('{"target_appointment_id": null, "target_expected_version": null}', 1, False),
        ('{"target_appointment_id": "7", "target_expected_version": "7"}', 7, False),
        ('{"target_appointment_id": 1, "target_expected_version": 1}', 1, True),
        (
            '{"target_appointment_id": 2147483647, "target_expected_version": 2147483647}',
            2147483647,
            True,
        ),
        ('{"target_appointment_id": 7, "target_expected_version": 7}', 8, False),
        ('{"target_appointment_id": 0, "target_expected_version": 0}', 1, False),
        ('{"target_appointment_id": -1, "target_expected_version": -1}', 1, False),
        ('{"target_appointment_id": 1.0, "target_expected_version": 1.0}', 1, False),
        (
            '{"target_appointment_id": 2147483648, "target_expected_version": 2147483648}',
            1,
            False,
        ),
        (
            '{"target_appointment_id": 10000000000000000000000000000000000000000, '
            '"target_expected_version": 10000000000000000000000000000000000000000}',
            1,
            False,
        ),
        (
            '{"target_appointment_id": -10000000000000000000000000000000000000000, '
            '"target_expected_version": -10000000000000000000000000000000000000000}',
            1,
            False,
        ),
        ('{"target_appointment_id": true, "target_expected_version": true}', 1, False),
        ('{"target_appointment_id": {}, "target_expected_version": {}}', 1, False),
        ('{"target_appointment_id": [], "target_expected_version": []}', 1, False),
        ('{"target_appointment_id": 1, "target_expected_version": 1}', None, False),
    ],
    ids=[
        "sql-null-payload",
        "non-object",
        "missing",
        "unknown-field",
        "json-null",
        "numeric-string",
        "minimum",
        "maximum",
        "mismatch",
        "zero",
        "negative",
        "decimal",
        "above-maximum",
        "positive-overflow",
        "negative-overflow",
        "boolean",
        "object",
        "array",
        "sql-null-expected",
    ],
)
async def test_appointment_payload_positive_integer_matcher_is_total(
    pg_engine: AsyncEngine,
    field_name: str,
    payload: str | None,
    expected_value: int | None,
    expected_result: bool,
) -> None:
    async with pg_engine.connect() as connection:
        result = await connection.scalar(
            text(
                "SELECT appointment_payload_positive_integer_matches("
                "CAST(:payload AS jsonb), CAST(:field_name AS text), "
                "CAST(:expected_value AS integer))"
            ),
            {
                "payload": payload,
                "field_name": field_name,
                "expected_value": expected_value,
            },
        )
    assert result is expected_result
    assert result is not None


@pytest.mark.parametrize("field_name", [None, ""])
async def test_appointment_payload_positive_integer_matcher_rejects_invalid_field_name(
    pg_engine: AsyncEngine,
    field_name: str | None,
) -> None:
    async with pg_engine.connect() as connection:
        result = await connection.scalar(
            text(
                "SELECT appointment_payload_positive_integer_matches("
                "'{\"target_appointment_id\": 1}'::jsonb, "
                "CAST(:field_name AS text), 1)"
            ),
            {"field_name": field_name},
        )
    assert result is False
    assert result is not None


async def _exercise_creation_call_id_provenance(
    session: AsyncSession,
    *,
    payload_call_id: object,
    appointment_call_id: int | None,
    missing_key: bool = False,
) -> None:
    await _seed_head_catalog(session)
    if appointment_call_id is not None:
        await session.execute(
            text("INSERT INTO calls (id, business_id) VALUES (:id, 1)"),
            {"id": appointment_call_id},
        )
    payload = _appointment_payload(
        1,
        start_at=START,
        end_at=START + timedelta(minutes=30),
    )
    data = payload["data"]
    assert isinstance(data, dict)
    if missing_key:
        data.pop("call_id")
    else:
        data["call_id"] = payload_call_id
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "payload_digest, status, expires_at, idempotency_key, committed_entity_type, "
            "committed_entity_id, version) VALUES "
            "(1, 1, 'appointment', 1, CAST(:payload AS jsonb), :digest, 'confirmed', "
            "now() + interval '15 minutes', 'call-id-create', 'appointment', 1, 1)"
        ),
        {"payload": json.dumps(payload), "digest": "2" * 64},
    )
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_phone, call_id, start_at, end_at, "
            "effective_start_at, effective_end_at, service_name_snapshot, "
            "resource_name_snapshot, duration_minutes_snapshot, "
            "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
            "business_timezone_snapshot, status, source, "
            "idempotency_key, pending_action_id, version, created_at, updated_at) VALUES "
            "(1, 1, 1, 1, '+919123456789', :call_id, :start, :end, :start, :end, "
            "'Haircut', 'Priya', 30, 0, 0, 'Asia/Kolkata', 'confirmed', "
            "'customer_conversation', "
            "'call-id-appointment', 1, 1, now(), now())"
        ),
        {
            "call_id": appointment_call_id,
            "start": START,
            "end": START + timedelta(minutes=30),
        },
    )
    await session.execute(
        text(
            "INSERT INTO resource_allocations "
            "(business_id, resource_id, appointment_id, pending_action_id, allocation_type, "
            "source, effective_start_at, effective_end_at, idempotency_key, version) VALUES "
            "(1, 1, 1, 1, 'appointment', 'customer_conversation', :start, :end, "
            "'call-id-allocation', 1)"
        ),
        {"start": START, "end": START + timedelta(minutes=30)},
    )


@pytest.mark.parametrize(
    ("payload_call_id", "appointment_call_id"),
    [(7, 7), (None, None)],
    ids=["matching-number", "matching-null"],
)
async def test_creation_call_id_provenance_accepts_canonical_match(
    pg_session_factory: async_sessionmaker[AsyncSession],
    payload_call_id: object,
    appointment_call_id: int | None,
) -> None:
    async with pg_session_factory() as session:
        await _exercise_creation_call_id_provenance(
            session,
            payload_call_id=payload_call_id,
            appointment_call_id=appointment_call_id,
        )
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await session.rollback()


@pytest.mark.parametrize(
    ("payload_call_id", "appointment_call_id", "missing_key"),
    [
        (None, None, True),
        ("7", 7, False),
        (None, 7, False),
        (7, None, False),
        (7, 8, False),
        (1.0, 1, False),
        (2147483648, None, False),
        ({}, None, False),
        ([], None, False),
        (True, None, False),
    ],
    ids=[
        "missing",
        "numeric-string",
        "null-versus-number",
        "number-versus-null",
        "number-mismatch",
        "decimal",
        "overflow",
        "object",
        "array",
        "boolean",
    ],
)
async def test_creation_call_id_provenance_rejects_noncanonical_or_mismatch(
    pg_session_factory: async_sessionmaker[AsyncSession],
    payload_call_id: object,
    appointment_call_id: int | None,
    missing_key: bool,
) -> None:
    async with pg_session_factory() as session:
        await _exercise_creation_call_id_provenance(
            session,
            payload_call_id=payload_call_id,
            appointment_call_id=appointment_call_id,
            missing_key=missing_key,
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        assert _pg_constraint_name(error.value) == (
            "ck_customer_conversation_appointment_provenance"
        )
        await session.rollback()


async def test_representable_0004_downgrades_and_reupgrades_without_fact_loss(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    await _prepare_0003(pg_engine, postgres_database_url)
    async with pg_engine.begin() as connection:
        await _seed_0003_appointment(connection, 1, start_at=START)
    _run_alembic(postgres_database_url, "upgrade", "0004")
    _run_alembic(postgres_database_url, "downgrade", "0003")

    async with pg_engine.connect() as connection:
        legacy = (
            await connection.execute(
                text(
                    "SELECT service_id, resource_id, start_at, end_at, status, "
                    "pending_action_id, held_until FROM appointments WHERE id = 1"
                )
            )
        ).one()
    assert legacy.service_id == 1
    assert legacy.resource_id == 1
    assert legacy.start_at == START
    assert legacy.end_at == START + timedelta(minutes=30)
    assert legacy.status == "confirmed"
    assert legacy.pending_action_id == 1
    assert legacy.held_until is None

    _run_alembic(postgres_database_url, "upgrade", "0004")
    async with pg_engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        allocation_count = await connection.scalar(
            text("SELECT count(*) FROM resource_allocations WHERE appointment_id = 1")
        )
    assert revision == "0004"
    assert allocation_count == 1


def _appointment_payload_facts(start_at: datetime) -> dict[str, object]:
    end_at = start_at + timedelta(minutes=30)
    return {
        "service_id": 1,
        "service_name": "Haircut",
        "resource_id": 1,
        "resource_name": "Priya",
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "effective_start_at": start_at.isoformat(),
        "effective_end_at": end_at.isoformat(),
        "duration_minutes": 30,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 0,
        "price": None,
        "business_timezone": "Asia/Kolkata",
    }


async def _insert_confirmed_mutation_action(
    session: AsyncSession,
    *,
    action_id: int,
    commit_id: int,
    operation: str,
    expected_version: int = 1,
    old_start: datetime = START,
    new_start: datetime | None = None,
) -> None:
    data: dict[str, object] = {
        "operation": operation,
        "target_appointment_id": 1,
        "target_expected_version": expected_version,
    }
    if operation == "cancel":
        data.update(current_facts=_appointment_payload_facts(old_start), reason_code=None)
    else:
        assert new_start is not None
        data.update(
            old_facts=_appointment_payload_facts(old_start),
            new_facts=_appointment_payload_facts(new_start),
        )
    envelope = {"schema_version": 1, "action_type": "appointment", "data": data}
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "payload_digest, status, expires_at, idempotency_key, committed_entity_type, "
            "committed_entity_id, version) VALUES "
            "(:id, 1, 'appointment', 1, CAST(:payload AS jsonb), :digest, 'confirmed', "
            "now() + interval '15 minutes', :key, 'appointment_commit', :commit_id, 1)"
        ),
        {
            "id": action_id,
            "payload": json.dumps(envelope),
            "digest": f"{action_id:064x}",
            "key": f"mutation-{action_id}",
            "commit_id": commit_id,
        },
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service_id", "not-an-id"),
        ("resource_id", 10**100),
        ("start_at", "not-a-timestamp"),
        ("end_at", "2026-99-99T00:00:00+00:00"),
        ("call_id", 10**100),
    ],
)
async def test_malformed_runtime_creation_provenance_is_a_constraint_violation(
    pg_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    value: object,
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        payload = _appointment_payload(
            1,
            start_at=START,
            end_at=START + timedelta(minutes=30),
        )
        data = payload["data"]
        assert isinstance(data, dict)
        if field == "call_id":
            data[field] = value
        else:
            facts = data["facts"]
            assert isinstance(facts, dict)
            facts[field] = value
        await session.execute(
            text(
                "INSERT INTO pending_actions "
                "(id, business_id, action_type, payload_schema_version, proposed_payload, "
                "payload_digest, status, expires_at, idempotency_key, committed_entity_type, "
                "committed_entity_id, version) VALUES "
                "(1, 1, 'appointment', 1, CAST(:payload AS jsonb), :digest, 'confirmed', "
                "now() + interval '15 minutes', 'runtime-create', 'appointment', 1, 1)"
            ),
            {"payload": json.dumps(payload), "digest": "1" * 64},
        )
        await session.execute(
            text(
                "INSERT INTO appointments "
                "(id, business_id, resource_id, service_id, customer_phone, start_at, end_at, "
                "effective_start_at, effective_end_at, service_name_snapshot, "
                "resource_name_snapshot, duration_minutes_snapshot, "
                "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
                "business_timezone_snapshot, status, source, "
                "idempotency_key, pending_action_id, version, created_at, updated_at) VALUES "
                "(1, 1, 1, 1, '+919123456789', :start, :end, :start, :end, "
                "'Haircut', 'Priya', 30, 0, 0, 'Asia/Kolkata', 'confirmed', "
                "'customer_conversation', "
                "'runtime-appointment', 1, 1, now(), now())"
            ),
            {"start": START, "end": START + timedelta(minutes=30)},
        )
        await session.execute(
            text(
                "INSERT INTO resource_allocations "
                "(business_id, resource_id, appointment_id, pending_action_id, allocation_type, "
                "source, effective_start_at, effective_end_at, idempotency_key, version) VALUES "
                "(1, 1, 1, 1, 'appointment', 'customer_conversation', :start, :end, "
                "'runtime-allocation', 1)"
            ),
            {"start": START, "end": START + timedelta(minutes=30)},
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        assert _pg_constraint_name(error.value) == (
            "ck_customer_conversation_appointment_provenance"
        )
        await session.rollback()


@pytest.mark.parametrize("field", ["target_appointment_id", "target_expected_version"])
@pytest.mark.parametrize(
    ("value", "remove_field"),
    [
        (None, True),
        (None, False),
        ("7", False),
        (0, False),
        (-1, False),
        (1.0, False),
        (2147483648, False),
        (10**100, False),
        (True, False),
        ({}, False),
        ([], False),
        (2, False),
    ],
    ids=[
        "missing",
        "null",
        "numeric-string",
        "zero",
        "negative",
        "decimal",
        "above-maximum",
        "overflow",
        "boolean",
        "object",
        "array",
        "mismatch",
    ],
)
async def test_malformed_runtime_commit_provenance_is_a_constraint_violation(
    pg_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    value: object,
    remove_field: bool,
) -> None:
    past = datetime(2020, 1, 1, 10, tzinfo=UTC)
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=past, status="completed")
        await _insert_confirmed_mutation_action(
            session,
            action_id=10,
            commit_id=10,
            operation="cancel",
        )
        if remove_field:
            await session.execute(
                text(
                    "UPDATE pending_actions SET proposed_payload = "
                    "jsonb_set(proposed_payload, '{data}', "
                    "proposed_payload->'data' - :field) WHERE id = 10"
                ),
                {"field": field},
            )
        else:
            await session.execute(
                text(
                    "UPDATE pending_actions SET proposed_payload = "
                    "jsonb_set(proposed_payload, CAST(:path AS text[]), CAST(:value AS jsonb)) "
                    "WHERE id = 10"
                ),
                {"path": ["data", field], "value": json.dumps(value)},
            )
        await session.execute(
            text(
                "INSERT INTO appointment_commits "
                "(id, business_id, pending_action_id, appointment_id, operation, "
                "before_snapshot, after_snapshot) VALUES "
                "(10, 1, 10, 1, 'cancel', '{}'::jsonb, '{}'::jsonb)"
            )
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        assert _pg_constraint_name(error.value) == ("ck_appointment_commit_provenance")
        await session.rollback()


async def test_reschedule_requires_exact_commit_and_allows_in_place_fact_update(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shifted_start = START + timedelta(hours=1)
    shifted_end = shifted_start + timedelta(minutes=30)
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await session.commit()

    async with pg_session_factory() as session:
        before_snapshot = await session.scalar(
            text("SELECT appointment_authoritative_snapshot(a) FROM appointments a WHERE id = 1")
        )
        await _insert_confirmed_mutation_action(
            session,
            action_id=10,
            commit_id=10,
            operation="reschedule",
            new_start=shifted_start,
        )
        await session.execute(
            text(
                "UPDATE appointments SET start_at=:start, end_at=:end, "
                "effective_start_at=:start, effective_end_at=:end, rescheduled_at=now(), "
                "version=2 WHERE id=1"
            ),
            {"start": shifted_start, "end": shifted_end},
        )
        await session.execute(
            text(
                "UPDATE resource_allocations SET status='released', "
                "version=2 WHERE appointment_id=1"
            ),
        )
        await session.execute(
            text(
                "INSERT INTO resource_allocations "
                "(business_id, resource_id, appointment_id, allocation_type, status, source, "
                "effective_start_at, effective_end_at, idempotency_key, version) VALUES "
                "(1, 1, 1, 'manual_appointment', 'active', 'owner_manual', "
                ":start, :end, 'rescheduled-allocation-1', 1)"
            ),
            {"start": shifted_start, "end": shifted_end},
        )
        after_snapshot = await session.scalar(
            text("SELECT appointment_authoritative_snapshot(a) FROM appointments a WHERE id = 1")
        )
        await session.execute(
            text(
                "INSERT INTO appointment_commits "
                "(id, business_id, pending_action_id, appointment_id, operation, "
                "before_snapshot, after_snapshot) VALUES "
                "(10, 1, 10, 1, 'reschedule', CAST(:before AS jsonb), CAST(:after AS jsonb))"
            ),
            {"before": json.dumps(before_snapshot), "after": json.dumps(after_snapshot)},
        )
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await session.commit()


async def test_cancel_requires_exact_commit_and_allows_status_transition(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await session.commit()

    async with pg_session_factory() as session:
        before_snapshot = await session.scalar(
            text("SELECT appointment_authoritative_snapshot(a) FROM appointments a WHERE id = 1")
        )
        await _insert_confirmed_mutation_action(
            session,
            action_id=10,
            commit_id=10,
            operation="cancel",
        )
        await session.execute(
            text(
                "UPDATE appointments SET status='cancelled', cancelled_at=now(), "
                "version=2 WHERE id=1"
            )
        )
        await session.execute(
            text(
                "UPDATE resource_allocations SET status='released', version=2 "
                "WHERE appointment_id=1"
            )
        )
        after_snapshot = await session.scalar(
            text("SELECT appointment_authoritative_snapshot(a) FROM appointments a WHERE id = 1")
        )
        await session.execute(
            text(
                "INSERT INTO appointment_commits "
                "(id, business_id, pending_action_id, appointment_id, operation, "
                "before_snapshot, after_snapshot) VALUES "
                "(10, 1, 10, 1, 'cancel', CAST(:before AS jsonb), CAST(:after AS jsonb))"
            ),
            {"before": json.dumps(before_snapshot), "after": json.dumps(after_snapshot)},
        )
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await session.commit()


@pytest.mark.parametrize("field", ["target_appointment_id", "target_expected_version"])
@pytest.mark.parametrize("value", ["1", 1.0], ids=["numeric-string", "decimal"])
async def test_noncanonical_target_does_not_match_mutation_commit(
    pg_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    value: object,
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await session.commit()

    async with pg_session_factory() as session:
        before_snapshot = await session.scalar(
            text("SELECT appointment_authoritative_snapshot(a) FROM appointments a WHERE id = 1")
        )
        await _insert_confirmed_mutation_action(
            session,
            action_id=10,
            commit_id=10,
            operation="cancel",
        )
        await session.execute(
            text(
                "UPDATE pending_actions SET proposed_payload = "
                "jsonb_set(proposed_payload, CAST(:path AS text[]), CAST(:value AS jsonb)) "
                "WHERE id = 10"
            ),
            {"path": ["data", field], "value": json.dumps(value)},
        )
        await session.execute(
            text(
                "UPDATE appointments SET status='cancelled', cancelled_at=now(), "
                "version=2 WHERE id=1"
            )
        )
        await session.execute(
            text(
                "UPDATE resource_allocations SET status='cancelled', version=2 "
                "WHERE appointment_id=1"
            )
        )
        after_snapshot = await session.scalar(
            text("SELECT appointment_authoritative_snapshot(a) FROM appointments a WHERE id = 1")
        )
        await session.execute(
            text(
                "INSERT INTO appointment_commits "
                "(id, business_id, pending_action_id, appointment_id, operation, "
                "before_snapshot, after_snapshot) VALUES "
                "(10, 1, 10, 1, 'cancel', CAST(:before AS jsonb), CAST(:after AS jsonb))"
            ),
            {"before": json.dumps(before_snapshot), "after": json.dumps(after_snapshot)},
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ck_appointment_mutation_commit IMMEDIATE"))
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        assert _pg_constraint_name(error.value) == ("ck_appointment_mutation_commit")
        await session.rollback()


async def test_confirmed_cancel_pending_action_without_commit_is_rejected(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=START)
        await _insert_active_allocation(session, 1, start_at=START)
        await _insert_confirmed_mutation_action(
            session, action_id=10, commit_id=10, operation="cancel"
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert _pg_constraint_name(error.value) == ("ck_confirmed_appointment_action_commit")
        await session.rollback()


async def test_terminal_rewrite_and_appointment_delete_are_rejected(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    past = datetime(2020, 1, 1, 10, tzinfo=UTC)
    async with pg_session_factory() as session:
        await _seed_head_catalog(session)
        await _insert_head_appointment(session, 1, start_at=past, status="completed")
        await session.commit()
    async with pg_session_factory() as session:
        await session.execute(
            text("UPDATE appointments SET customer_phone='+919999999999', version=2 WHERE id=1")
        )
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert _pg_constraint_name(error.value) == "ck_appointment_mutation_commit"
        await session.rollback()
    async with pg_session_factory() as session:
        await session.execute(text("DELETE FROM appointments WHERE id=1"))
        with pytest.raises(IntegrityError) as error:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert _pg_constraint_name(error.value) == "ck_appointment_mutation_commit"
        await session.rollback()
