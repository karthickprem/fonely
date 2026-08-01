"""pending_action_state_machine

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-01

Adds a stable SHA-256 digest used for tenant-scoped PendingAction idempotency
comparison and a safe machine-readable rejection reason.

Online upgrades validate and backfill populated 0001 rows before enforcing
NOT NULL. Offline SQL aborts if rows exist because row-dependent Python
canonicalization cannot be safely represented as static SQL.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from alembic import context, op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOWED_TOP_LEVEL = {"schema_version", "action_type", "data"}
_ORDER_DATA_FIELDS = {
    "customer_name",
    "customer_phone",
    "pickup_at",
    "lines",
    "customer_note",
}
_ORDER_LINE_FIELDS = {"product_id", "quantity"}
_STOCK_DATA_FIELDS = {"product_id", "business_date", "operation", "quantity", "note"}


def _require_exact_fields(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise RuntimeError(f"Unsupported {label} fields in pending action migration")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise RuntimeError("Invalid decimal value in pending action migration")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise RuntimeError("Invalid decimal value in pending action migration") from exc
    if not result.is_finite() or result <= 0 or result != result.quantize(Decimal("0.01")):
        raise RuntimeError("Invalid quantity precision in pending action migration")
    return result


def _optional_text(value: Any, max_length: int, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise RuntimeError(f"Invalid {label} in pending action migration")
    return value


def _e164_phone(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", value):
        raise RuntimeError("Invalid customer phone in pending action migration")
    return value


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise RuntimeError("Invalid pickup datetime in pending action migration") from exc
    else:
        raise RuntimeError("Invalid pickup datetime in pending action migration")
    if result.tzinfo is None:
        raise RuntimeError("Naive pickup datetime in pending action migration")
    return result


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        raise RuntimeError("Invalid business date in pending action migration")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise RuntimeError("Invalid business date in pending action migration") from exc
    raise RuntimeError("Invalid business date in pending action migration")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _canonical_payload(
    action_type: str,
    schema_version: int,
    raw_payload: Mapping[str, Any],
) -> dict[str, Any]:
    if schema_version != 1:
        raise RuntimeError("Unsupported payload schema version in pending action migration")
    _require_exact_fields(raw_payload, _ALLOWED_TOP_LEVEL, "payload envelope")
    if raw_payload.get("schema_version") != schema_version:
        raise RuntimeError("Payload schema version mismatch in pending action migration")
    if raw_payload.get("action_type") != action_type:
        raise RuntimeError("Payload action type mismatch in pending action migration")
    raw_data = raw_payload.get("data")
    if not isinstance(raw_data, Mapping):
        raise RuntimeError("Invalid payload data in pending action migration")

    if action_type == "order":
        _require_exact_fields(raw_data, _ORDER_DATA_FIELDS, "order data")
        lines = raw_data.get("lines")
        if not isinstance(lines, list) or not lines:
            raise RuntimeError("Invalid order lines in pending action migration")
        canonical_lines: list[dict[str, Any]] = []
        seen_products: set[int] = set()
        for line in lines:
            if not isinstance(line, Mapping):
                raise RuntimeError("Invalid order line in pending action migration")
            _require_exact_fields(line, _ORDER_LINE_FIELDS, "order line")
            product_id = line.get("product_id")
            if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
                raise RuntimeError("Invalid product ID in pending action migration")
            if product_id in seen_products:
                raise RuntimeError("Duplicate product ID in pending action migration")
            seen_products.add(product_id)
            canonical_lines.append(
                {"product_id": product_id, "quantity": _decimal(line.get("quantity"))}
            )
        canonical_lines.sort(key=lambda line: line["product_id"])
        data = {
            "customer_name": _optional_text(raw_data.get("customer_name"), 200, "customer name"),
            "customer_phone": _e164_phone(raw_data.get("customer_phone")),
            "pickup_at": _aware_datetime(raw_data.get("pickup_at")),
            "lines": canonical_lines,
            "customer_note": _optional_text(raw_data.get("customer_note"), 500, "customer note"),
        }
    elif action_type == "owner_stock_update":
        _require_exact_fields(raw_data, _STOCK_DATA_FIELDS, "stock-update data")
        product_id = raw_data.get("product_id")
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise RuntimeError("Invalid product ID in pending action migration")
        operation = raw_data.get("operation")
        if operation not in {"set", "add"}:
            raise RuntimeError("Invalid stock operation in pending action migration")
        data = {
            "product_id": product_id,
            "business_date": _date(raw_data.get("business_date")),
            "operation": operation,
            "quantity": _decimal(raw_data.get("quantity")),
            "note": _optional_text(raw_data.get("note"), 500, "stock note"),
        }
    else:
        raise RuntimeError("Unsupported action type in pending action migration")

    return _canonical_value(
        {"schema_version": schema_version, "action_type": action_type, "data": data}
    )


def _digest(action_type: str, schema_version: int, raw_payload: Mapping[str, Any]) -> str:
    canonical = _canonical_payload(action_type, schema_version, raw_payload)
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _backfill_online() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, action_type, payload_schema_version, proposed_payload "
            "FROM pending_actions ORDER BY id"
        )
    ).mappings()
    for row in rows:
        raw_payload = row["proposed_payload"]
        if not isinstance(raw_payload, Mapping):
            raise RuntimeError("Invalid stored payload in pending action migration")
        digest = _digest(
            str(row["action_type"]),
            int(row["payload_schema_version"]),
            raw_payload,
        )
        bind.execute(
            sa.text("UPDATE pending_actions SET payload_digest=:digest WHERE id=:id"),
            {"digest": digest, "id": row["id"]},
        )


def upgrade() -> None:
    op.add_column(
        "pending_actions",
        sa.Column("payload_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "pending_actions",
        sa.Column("rejection_reason_code", sa.String(length=50), nullable=True),
    )
    if context.is_offline_mode():
        op.execute(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pending_actions) THEN "
            "RAISE EXCEPTION 'Migration 0002 requires online backfill for populated "
            "pending_actions'; "
            "END IF; END $$"
        )
    else:
        _backfill_online()
    op.alter_column("pending_actions", "payload_digest", nullable=False)


def downgrade() -> None:
    op.drop_column("pending_actions", "rejection_reason_code")
    op.drop_column("pending_actions", "payload_digest")
