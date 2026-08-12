"""Strict payload and command-boundary tests."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fonely.domain.pending_actions.commands import (
    ActorContext,
    CommitResultContext,
    CreatePendingActionCommand,
    FailCommitCommand,
)
from fonely.domain.pending_actions.errors import UnsupportedPayloadSchemaError
from fonely.domain.pending_actions.payloads import (
    OwnerStockUpdateEnvelope,
    PendingOrderEnvelope,
    validate_payload,
)
from fonely.models.enums import CallerRole, Channel, PendingActionType

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def order_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": "Example Customer",
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00Z",
            "lines": [{"product_id": 10, "quantity": "2.00"}],
            "customer_note": "Cut into small pieces",
        },
    }


def stock_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "owner_stock_update",
        "data": {
            "product_id": 10,
            "business_date": "2026-08-01",
            "operation": "set",
            "quantity": "5.00",
            "note": "Morning stock",
        },
    }


def actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
        channel=Channel.TEXT,
        session_id="session-1",
    )


class TestPayloadRegistry:
    def test_valid_order_payload(self) -> None:
        result = validate_payload(PendingActionType.ORDER, 1, order_payload())
        assert isinstance(result, PendingOrderEnvelope)
        assert result.data.lines[0].quantity == Decimal("2.00")

    def test_valid_stock_payload(self) -> None:
        result = validate_payload(
            PendingActionType.OWNER_STOCK_UPDATE,
            1,
            stock_payload(),
        )
        assert isinstance(result, OwnerStockUpdateEnvelope)
        assert result.data.business_date == date(2026, 8, 1)

    def test_unknown_schema_version_rejected(self) -> None:
        with pytest.raises(UnsupportedPayloadSchemaError):
            validate_payload(PendingActionType.ORDER, 2, order_payload())

    def test_action_type_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_payload(
                PendingActionType.ORDER,
                1,
                stock_payload(),
            )

    def test_unknown_fields_rejected(self) -> None:
        payload = order_payload()
        payload["unexpected"] = True
        with pytest.raises(ValidationError):
            validate_payload(PendingActionType.ORDER, 1, payload)

    def test_empty_order_lines_rejected(self) -> None:
        payload = order_payload()
        data = payload["data"]
        assert isinstance(data, dict)
        data["lines"] = []
        with pytest.raises(ValidationError):
            validate_payload(PendingActionType.ORDER, 1, payload)

    def test_duplicate_products_rejected(self) -> None:
        payload = order_payload()
        data = payload["data"]
        assert isinstance(data, dict)
        data["lines"] = [
            {"product_id": 10, "quantity": "1.00"},
            {"product_id": 10, "quantity": "2.00"},
        ]
        with pytest.raises(ValidationError, match="Duplicate product"):
            validate_payload(PendingActionType.ORDER, 1, payload)

    def test_float_quantity_rejected(self) -> None:
        payload = order_payload()
        data = payload["data"]
        assert isinstance(data, dict)
        data["lines"] = [{"product_id": 10, "quantity": 2.0}]
        with pytest.raises(ValidationError, match="Float is not accepted"):
            validate_payload(PendingActionType.ORDER, 1, payload)

    def test_naive_pickup_datetime_rejected(self) -> None:
        payload = order_payload()
        data = payload["data"]
        assert isinstance(data, dict)
        data["pickup_at"] = datetime(2026, 8, 1, 10, 0)
        with pytest.raises(ValidationError, match="timezone-aware"):
            validate_payload(PendingActionType.ORDER, 1, payload)

    def test_authoritative_price_fields_rejected(self) -> None:
        payload = order_payload()
        data = payload["data"]
        assert isinstance(data, dict)
        data["total"] = "100.00"
        with pytest.raises(ValidationError):
            validate_payload(PendingActionType.ORDER, 1, payload)


class TestCommandBoundary:
    def test_create_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            CreatePendingActionCommand(
                actor=actor(),
                action_type=PendingActionType.ORDER,
                payload_schema_version=1,
                payload=order_payload(),
                expires_at=NOW + timedelta(minutes=15),
                idempotency_key="key-1",
                status="confirmed",  # type: ignore[call-arg]
            )

    def test_fail_commit_rejects_unknown_error_code(self) -> None:
        with pytest.raises(ValidationError):
            FailCommitCommand(
                context=CommitResultContext(
                    business_id=1,
                    pending_action_id=1,
                    expected_version=2,
                    engine="order_engine",
                ),
                error_code="raw_sql_error",  # type: ignore[arg-type]
                retryable=True,
            )

    def test_commit_context_rejects_unknown_engine(self) -> None:
        with pytest.raises(ValidationError):
            CommitResultContext(
                business_id=1,
                pending_action_id=1,
                expected_version=2,
                engine="caller",  # type: ignore[arg-type]
            )
