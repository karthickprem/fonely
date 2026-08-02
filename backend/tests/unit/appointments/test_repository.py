"""Unit tests for appointment repository tenant scoping."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from fonely.repositories.appointments import AppointmentRepository


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def repo(mock_session: AsyncMock) -> AppointmentRepository:
    return AppointmentRepository(mock_session)


async def test_get_by_business_and_pending_action_scopes_by_business(
    repo: AppointmentRepository,
    mock_session: AsyncMock,
) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    result = await repo.get_by_business_and_pending_action(1, 10)

    assert result is None
    mock_session.execute.assert_called_once()
    stmt = mock_session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "business_id" in compiled


async def test_insert_flushes_without_commit(
    repo: AppointmentRepository,
    mock_session: AsyncMock,
) -> None:
    await repo.insert({"business_id": 1, "resource_id": 1})

    mock_session.add.assert_called_once()
    mock_session.flush.assert_called_once()
    mock_session.commit.assert_not_called()


async def test_insert_allocation_flushes_without_commit(
    repo: AppointmentRepository,
    mock_session: AsyncMock,
) -> None:
    await repo.insert_allocation({"business_id": 1, "resource_id": 1})

    mock_session.add.assert_called_once()
    mock_session.flush.assert_called_once()
    mock_session.commit.assert_not_called()


async def test_force_constraints_executes_sql(
    repo: AppointmentRepository,
    mock_session: AsyncMock,
) -> None:
    await repo.force_constraints("SET CONSTRAINTS ck_test IMMEDIATE")

    mock_session.execute.assert_called_once()
