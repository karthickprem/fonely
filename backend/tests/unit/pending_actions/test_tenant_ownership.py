"""Tenant-scoped product ownership validation tests."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from fonely.domain.pending_actions.errors import PendingActionNotFoundError
from fonely.domain.pending_actions.payloads import validate_payload
from fonely.models.enums import PendingActionType
from fonely.services.pending_actions import PendingActionService


def order_payload(*product_ids: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_phone": "+919123456789",
            "pickup_at": datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
            "lines": [{"product_id": product_id, "quantity": "1.00"} for product_id in product_ids],
        },
    }


def service_with_owned_ids(*owned_ids: int) -> PendingActionService:
    scalar_result = AsyncMock()
    scalar_result.all = lambda: list(owned_ids)
    session = AsyncMock()
    session.scalars.return_value = scalar_result
    return PendingActionService(session)


async def test_all_active_products_owned_by_business_pass() -> None:
    payload = validate_payload(PendingActionType.ORDER, 1, order_payload(10, 20))
    await service_with_owned_ids(10, 20)._validate_new_payload_products(1, payload)


async def test_missing_or_cross_tenant_active_product_fails_without_disclosing_id() -> None:
    payload = validate_payload(PendingActionType.ORDER, 1, order_payload(10, 20))
    with pytest.raises(PendingActionNotFoundError, match="products were not found"):
        await service_with_owned_ids(10)._validate_new_payload_products(1, payload)


async def test_stored_payload_requires_ownership_but_not_active_status() -> None:
    payload = validate_payload(PendingActionType.ORDER, 1, order_payload(10))
    service = service_with_owned_ids(10)
    await service._validate_stored_payload_ownership(1, payload)
    statement = service._session.scalars.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "products.business_id" in sql
    assert "products.id IN" in sql
    assert "products.is_active" not in sql


async def test_new_payload_requires_active_product() -> None:
    payload = validate_payload(PendingActionType.ORDER, 1, order_payload(10))
    service = service_with_owned_ids(10)
    await service._validate_new_payload_products(1, payload)
    statement = service._session.scalars.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "products.is_active" in sql


async def test_owner_stock_update_product_is_tenant_scoped() -> None:
    payload = validate_payload(
        PendingActionType.OWNER_STOCK_UPDATE,
        1,
        {
            "schema_version": 1,
            "action_type": "owner_stock_update",
            "data": {
                "product_id": 30,
                "business_date": "2026-08-01",
                "operation": "set",
                "quantity": "5.00",
            },
        },
    )
    with pytest.raises(PendingActionNotFoundError):
        await service_with_owned_ids()._validate_new_payload_products(1, payload)
