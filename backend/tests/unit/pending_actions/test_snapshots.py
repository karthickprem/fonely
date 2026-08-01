"""Canonical digest and confirmation snapshot tests."""

import json
from copy import deepcopy

from fonely.domain.pending_actions.payloads import PendingOrderEnvelope
from fonely.domain.pending_actions.snapshots import (
    canonical_json,
    confirmation_snapshot,
    idempotency_matches,
    payload_digest,
)


def payload(quantity: str = "2.00") -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_type": "order",
        "data": {
            "customer_name": "Example Customer",
            "customer_phone": "+919123456789",
            "pickup_at": "2026-08-01T10:00:00+00:00",
            "lines": [{"product_id": 7, "quantity": quantity}],
            "customer_note": None,
        },
    }


def test_dictionary_key_order_does_not_change_digest() -> None:
    first = PendingOrderEnvelope.model_validate(payload())
    reordered = {
        "data": payload()["data"],
        "action_type": "order",
        "schema_version": 1,
    }
    second = PendingOrderEnvelope.model_validate(reordered)
    assert payload_digest(first) == payload_digest(second)
    assert canonical_json(first) == canonical_json(second)


def test_equivalent_decimal_format_does_not_change_digest() -> None:
    first = PendingOrderEnvelope.model_validate(payload("2.00"))
    second = PendingOrderEnvelope.model_validate(payload("2"))
    assert payload_digest(first) == payload_digest(second)


def test_equivalent_timezone_instants_do_not_change_digest() -> None:
    first = PendingOrderEnvelope.model_validate(payload())
    alternate = payload()
    data = alternate["data"]
    assert isinstance(data, dict)
    data["pickup_at"] = "2026-08-01T15:30:00+05:30"
    second = PendingOrderEnvelope.model_validate(alternate)
    assert payload_digest(first) == payload_digest(second)


def test_material_change_changes_digest() -> None:
    first = PendingOrderEnvelope.model_validate(payload("2.00"))
    second = PendingOrderEnvelope.model_validate(payload("3.00"))
    assert payload_digest(first) != payload_digest(second)


def test_order_line_order_does_not_change_digest_or_snapshot() -> None:
    first_raw = payload()
    first_data = first_raw["data"]
    assert isinstance(first_data, dict)
    first_data["lines"] = [
        {"product_id": 1, "quantity": "1.00"},
        {"product_id": 2, "quantity": "2.00"},
    ]
    second_raw = payload()
    second_data = second_raw["data"]
    assert isinstance(second_data, dict)
    second_data["lines"] = [
        {"product_id": 2, "quantity": "2.00"},
        {"product_id": 1, "quantity": "1.00"},
    ]
    first = PendingOrderEnvelope.model_validate(first_raw)
    second = PendingOrderEnvelope.model_validate(second_raw)
    assert payload_digest(first) == payload_digest(second)
    assert confirmation_snapshot(first) == confirmation_snapshot(second)
    assert [line.product_id for line in first.data.lines] == [1, 2]
    assert [line.product_id for line in second.data.lines] == [1, 2]


def test_idempotency_equivalence_requires_same_type_and_digest() -> None:
    digest = payload_digest(PendingOrderEnvelope.model_validate(payload()))
    assert idempotency_matches(
        existing_action_type="order",
        existing_digest=digest,
        proposed_action_type="order",
        proposed_digest=digest,
    )


def test_idempotency_conflicts_on_material_payload_change() -> None:
    first = payload_digest(PendingOrderEnvelope.model_validate(payload("2.00")))
    second = payload_digest(PendingOrderEnvelope.model_validate(payload("3.00")))
    assert not idempotency_matches(
        existing_action_type="order",
        existing_digest=first,
        proposed_action_type="order",
        proposed_digest=second,
    )


def test_idempotency_conflicts_on_action_type_change() -> None:
    digest = payload_digest(PendingOrderEnvelope.model_validate(payload()))
    assert not idempotency_matches(
        existing_action_type="order",
        existing_digest=digest,
        proposed_action_type="owner_stock_update",
        proposed_digest=digest,
    )


def test_confirmation_snapshot_is_deterministic_machine_readable_json() -> None:
    model = PendingOrderEnvelope.model_validate(payload())
    first = confirmation_snapshot(model)
    second = confirmation_snapshot(model)
    assert first == second
    decoded = json.loads(first)
    assert decoded["schema_version"] == 1
    assert decoded["action_type"] == "order"
    assert decoded["facts"]["lines"][0] == {"product_id": 7, "quantity": "2"}
    assert "price" not in decoded["facts"]
    assert "total" not in decoded["facts"]


def test_input_payload_not_mutated_by_canonicalization() -> None:
    raw = payload()
    original = deepcopy(raw)
    model = PendingOrderEnvelope.model_validate(raw)
    canonical_json(model)
    assert raw == original
