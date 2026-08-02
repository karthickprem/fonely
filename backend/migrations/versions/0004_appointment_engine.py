"""appointment_engine

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-01

Adds generic resource schedules, immutable appointment facts, and the single
resource-allocation capacity ledger. Populated upgrades and downgrades use an
online, sanitized representability preflight before any DDL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(values: tuple[str, ...], name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _fail_if_exists(sql: str, message: str) -> None:
    if op.get_bind().execute(sa.text(sql)).scalar_one_or_none() is not None:
        raise RuntimeError(message)


_AFFECTED_TABLES = (
    "appointment_commits",
    "appointments",
    "businesses",
    "calls",
    "operating_schedules",
    "pending_actions",
    "resource_allocations",
    "resources",
    "schedule_exceptions",
    "service_resource_eligibility",
    "services",
)


def _lock_affected_tables() -> None:
    """Serialize migration inspection and changes while allowing ordinary reads."""
    table_names = ", ".join(f"'{table_name}'" for table_name in _AFFECTED_TABLES)
    # SHARE ROW EXCLUSIVE conflicts with every normal writer lock and with another
    # migration taking the same lock, but not with ACCESS SHARE used by readers.
    # Missing tables are expected on upgrade; using one ordered union in both
    # directions avoids opposite-order deadlocks during operational recovery.
    op.execute(
        f"""DO $migration_lock$
        DECLARE
            table_name text;
        BEGIN
            FOREACH table_name IN ARRAY ARRAY[{table_names}] LOOP
                IF to_regclass(format('%I.%I', current_schema(), table_name)) IS NOT NULL THEN
                    EXECUTE format(
                        'LOCK TABLE %I.%I IN SHARE ROW EXCLUSIVE MODE',
                        current_schema(), table_name
                    );
                END IF;
            END LOOP;
        END
        $migration_lock$"""
    )


def _preflight_upgrade() -> None:
    """Reject every legacy state that has no deterministic approved conversion."""
    checks = (
        (
            """SELECT 1 FROM businesses b
               WHERE b.timezone IN ('Factory', 'localtime', 'posixrules')
                  OR b.timezone LIKE 'posix/%'
                  OR b.timezone LIKE 'right/%'
                  OR NOT EXISTS (
                      SELECT 1 FROM pg_timezone_names tz WHERE tz.name = b.timezone
                  )
               LIMIT 1""",
            "Migration 0004 found an invalid legacy business timezone",
        ),
        (
            """SELECT 1 FROM services
               WHERE duration_minutes IS NULL OR duration_minutes <= 0
                  OR duration_minutes > 720 LIMIT 1""",
            "Migration 0004 requires every service to have a supported duration",
        ),
        (
            "SELECT 1 FROM appointments WHERE pending_action_id IS NULL LIMIT 1",
            "Migration 0004 cannot determine appointment creation provenance",
        ),
        (
            """SELECT 1 FROM appointments a
               LEFT JOIN pending_actions p ON p.id = a.pending_action_id
               WHERE p.id IS NULL OR p.business_id IS DISTINCT FROM a.business_id
                  OR p.action_type IS DISTINCT FROM 'appointment'
                  OR p.payload_schema_version IS DISTINCT FROM 1
                  OR p.status IS DISTINCT FROM 'confirmed'
                  OR p.committed_entity_type IS DISTINCT FROM 'appointment'
                  OR p.committed_entity_id IS DISTINCT FROM a.id
                  OR jsonb_typeof(p.proposed_payload) IS DISTINCT FROM 'object'
                  OR p.proposed_payload->>'schema_version' IS DISTINCT FROM '1'
                  OR p.proposed_payload->>'action_type' IS DISTINCT FROM 'appointment'
                  OR jsonb_typeof(p.proposed_payload->'data') IS DISTINCT FROM 'object'
                  OR p.proposed_payload->'data'->>'operation' IS DISTINCT FROM 'create'
                  OR jsonb_typeof(p.proposed_payload->'data'->'facts') IS DISTINCT FROM 'object'
               LIMIT 1""",
            "Migration 0004 requires valid same-tenant appointment-create provenance",
        ),
        (
            """SELECT 1 FROM appointments a
               JOIN pending_actions p ON p.id = a.pending_action_id
               CROSS JOIN LATERAL (SELECT p.proposed_payload->'data' AS data) d
               CROSS JOIN LATERAL (SELECT d.data->'facts' AS facts) f
               WHERE jsonb_typeof(f.facts->'service_id') IS DISTINCT FROM 'number'
                  OR jsonb_typeof(f.facts->'service_name') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(f.facts->'resource_id') IS DISTINCT FROM 'number'
                  OR jsonb_typeof(f.facts->'resource_name') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(f.facts->'duration_minutes') IS DISTINCT FROM 'number'
                  OR jsonb_typeof(f.facts->'buffer_before_minutes') IS DISTINCT FROM 'number'
                  OR jsonb_typeof(f.facts->'buffer_after_minutes') IS DISTINCT FROM 'number'
                  OR jsonb_typeof(f.facts->'start_at') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(f.facts->'end_at') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(f.facts->'effective_start_at') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(f.facts->'effective_end_at') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(f.facts->'business_timezone') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(d.data->'customer_phone') IS DISTINCT FROM 'string'
                  OR (d.data ? 'call_id' AND d.data->'call_id' <> 'null'::jsonb
                      AND jsonb_typeof(d.data->'call_id') IS DISTINCT FROM 'number')
                  OR (f.facts ? 'price' AND f.facts->'price' <> 'null'::jsonb
                      AND jsonb_typeof(f.facts->'price') IS DISTINCT FROM 'string')
                  OR (f.facts ? 'price_display_text'
                      AND f.facts->'price_display_text' <> 'null'::jsonb
                      AND jsonb_typeof(f.facts->'price_display_text') IS DISTINCT FROM 'string')
               LIMIT 1""",
            "Migration 0004 found malformed appointment creation provenance",
        ),
        (
            r"""SELECT 1 FROM appointments a
               JOIN pending_actions p ON p.id = a.pending_action_id
               CROSS JOIN LATERAL (SELECT p.proposed_payload->'data' AS data) d
               CROSS JOIN LATERAL (SELECT d.data->'facts' AS facts) f
               WHERE COALESCE(f.facts->>'service_id' !~ '^[1-9][0-9]*$', TRUE)
                  OR NOT COALESCE(pg_input_is_valid(f.facts->>'service_id', 'bigint'), FALSE)
                  OR COALESCE(f.facts->>'resource_id' !~ '^[1-9][0-9]*$', TRUE)
                  OR NOT COALESCE(pg_input_is_valid(f.facts->>'resource_id', 'bigint'), FALSE)
                  OR COALESCE(f.facts->>'duration_minutes' !~ '^[1-9][0-9]*$', TRUE)
                  OR NOT COALESCE(
                         pg_input_is_valid(f.facts->>'duration_minutes', 'integer'), FALSE)
                  OR COALESCE(
                         f.facts->>'buffer_before_minutes' !~ '^(0|[1-9][0-9]*)$', TRUE)
                  OR NOT COALESCE(
                         pg_input_is_valid(f.facts->>'buffer_before_minutes', 'integer'), FALSE)
                  OR COALESCE(
                         f.facts->>'buffer_after_minutes' !~ '^(0|[1-9][0-9]*)$', TRUE)
                  OR NOT COALESCE(
                         pg_input_is_valid(f.facts->>'buffer_after_minutes', 'integer'), FALSE)
                  OR (d.data->'call_id' IS DISTINCT FROM 'null'::jsonb
                      AND (COALESCE(d.data->>'call_id' !~ '^[1-9][0-9]*$', TRUE)
                           OR NOT COALESCE(
                               pg_input_is_valid(d.data->>'call_id', 'bigint'), FALSE)))
                  OR (f.facts->'price' IS DISTINCT FROM 'null'::jsonb
                      AND (COALESCE(
                          f.facts->>'price' !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$', TRUE)
                           OR NOT COALESCE(
                               pg_input_is_valid(f.facts->>'price', 'numeric'), FALSE)
                           OR CASE WHEN pg_input_is_valid(f.facts->>'price', 'numeric')
                                   THEN (f.facts->>'price')::numeric < 0
                                       OR (f.facts->>'price')::numeric > 99999999.99
                                   ELSE TRUE END))
                  OR COALESCE(
                         f.facts->>'start_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$', TRUE)
                  OR COALESCE(
                         f.facts->>'end_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$', TRUE)
                  OR COALESCE(f.facts->>'effective_start_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$', TRUE)
                  OR COALESCE(f.facts->>'effective_end_at' !~* '(Z|[+-][0-9]{2}:[0-9]{2})$', TRUE)
                  OR NOT COALESCE(pg_input_is_valid(
                         f.facts->>'start_at', 'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                         f.facts->>'end_at', 'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                         f.facts->>'effective_start_at', 'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                         f.facts->>'effective_end_at', 'timestamp with time zone'), FALSE)
               LIMIT 1""",
            "Migration 0004 found invalid or offset-free appointment creation provenance",
        ),
        (
            """SELECT 1 FROM pending_actions p
               CROSS JOIN LATERAL (SELECT p.proposed_payload->'data' AS data) d
               WHERE p.action_type = 'appointment'
                 AND (jsonb_typeof(p.proposed_payload->'schema_version')
                          IS DISTINCT FROM 'number'
                      OR NOT COALESCE(pg_input_is_valid(
                             p.proposed_payload->>'schema_version', 'integer'), FALSE)
                      OR (d.data->>'operation' IN ('cancel', 'reschedule')
                          AND (jsonb_typeof(d.data->'target_appointment_id')
                                   IS DISTINCT FROM 'number'
                               OR NOT COALESCE(pg_input_is_valid(
                                      d.data->>'target_appointment_id', 'bigint'), FALSE))))
               LIMIT 1""",
            "Migration 0004 found invalid appointment version or target provenance",
        ),
        (
            """SELECT 1 FROM appointments a
               JOIN pending_actions p ON p.id = a.pending_action_id
               CROSS JOIN LATERAL (
                   SELECT p.proposed_payload->'data'->'facts' AS facts
               ) f
               WHERE f.facts->>'business_timezone' IS NULL
                  OR length(f.facts->>'business_timezone') > 50
                  OR f.facts->>'business_timezone' IN (
                      'Factory', 'localtime', 'posixrules')
                  OR f.facts->>'business_timezone' LIKE 'posix/%'
                  OR f.facts->>'business_timezone' LIKE 'right/%'
                  OR NOT EXISTS (
                      SELECT 1 FROM pg_timezone_names tz
                      WHERE tz.name = f.facts->>'business_timezone')
               LIMIT 1""",
            "Migration 0004 found invalid payload business timezone",
        ),
        (
            """SELECT 1 FROM appointments a
               JOIN pending_actions p ON p.id = a.pending_action_id
               LEFT JOIN businesses b ON b.id = a.business_id
               LEFT JOIN services s ON s.id = a.service_id
               LEFT JOIN resources r ON r.id = a.resource_id
               CROSS JOIN LATERAL (SELECT p.proposed_payload->'data' AS data) d
               CROSS JOIN LATERAL (SELECT d.data->'facts' AS facts) f
               WHERE b.id IS NULL OR s.id IS NULL OR r.id IS NULL
                  OR s.business_id IS DISTINCT FROM a.business_id
                  OR r.business_id IS DISTINCT FROM a.business_id
                  OR p.business_id IS DISTINCT FROM a.business_id
                  OR CASE WHEN pg_input_is_valid(f.facts->>'service_id', 'bigint')
                          THEN (f.facts->>'service_id')::bigint END
                        IS DISTINCT FROM a.service_id
                  OR CASE WHEN pg_input_is_valid(f.facts->>'resource_id', 'bigint')
                          THEN (f.facts->>'resource_id')::bigint END
                        IS DISTINCT FROM a.resource_id
                  OR CASE WHEN pg_input_is_valid(
                                   f.facts->>'start_at', 'timestamp with time zone')
                          THEN (f.facts->>'start_at')::timestamptz END
                        IS DISTINCT FROM a.start_at
                  OR CASE WHEN pg_input_is_valid(
                                   f.facts->>'end_at', 'timestamp with time zone')
                          THEN (f.facts->>'end_at')::timestamptz END
                        IS DISTINCT FROM a.end_at
                  OR d.data->>'customer_phone' IS DISTINCT FROM a.customer_phone
                  OR CASE WHEN d.data->'call_id' = 'null'::jsonb THEN NULL
                          WHEN pg_input_is_valid(d.data->>'call_id', 'bigint')
                          THEN (d.data->>'call_id')::bigint END
                        IS DISTINCT FROM a.call_id
               LIMIT 1""",
            "Migration 0004 found appointment provenance inconsistent with tenant "
            "or identity facts",
        ),
        (
            "SELECT 1 FROM appointments WHERE status = 'held' LIMIT 1",
            "Migration 0004 does not reinterpret legacy held appointments",
        ),
        (
            "SELECT 1 FROM appointments WHERE status = 'cancelled' LIMIT 1",
            "Migration 0004 cannot represent legacy cancelled appointments "
            "without authoritative cancellation evidence",
        ),
        (
            """SELECT 1 FROM appointments a
               WHERE a.end_at <= a.start_at
               LIMIT 1""",
            "Migration 0004 requires appointment end after start",
        ),
        (
            """SELECT 1 FROM appointments a
               LEFT JOIN calls c ON c.id = a.call_id
               WHERE a.call_id IS NOT NULL
                 AND (c.id IS NULL OR c.business_id IS DISTINCT FROM a.business_id)
               LIMIT 1""",
            "Migration 0004 found missing or cross-tenant call provenance",
        ),
        (
            """SELECT 1 FROM operating_schedules
               WHERE close_time <= open_time LIMIT 1""",
            "Migration 0004 found an unsupported operating schedule",
        ),
        (
            """SELECT 1 FROM schedule_exceptions
               WHERE NOT ((is_closed AND open_time IS NULL AND close_time IS NULL)
                  OR (NOT is_closed AND open_time IS NOT NULL AND close_time IS NOT NULL
                      AND close_time > open_time)) LIMIT 1""",
            "Migration 0004 found an inconsistent schedule exception",
        ),
        (
            """SELECT 1 FROM appointments a
               JOIN appointments b ON a.id < b.id
                AND a.business_id = b.business_id
                AND a.resource_id = b.resource_id
                AND a.status IN ('confirmed', 'completed', 'no_show')
                AND b.status IN ('confirmed', 'completed', 'no_show')
               WHERE tstzrange(a.start_at, a.end_at, '[)') &&
                     tstzrange(b.start_at, b.end_at, '[)')
               LIMIT 1""",
            "Migration 0004 found overlapping capacity-bearing legacy appointments",
        ),
    )
    for sql, message in checks:
        _fail_if_exists(sql, message)


def _backfill_appointment_facts() -> None:
    op.get_bind().execute(
        sa.text(
            """UPDATE appointments a
               SET service_name_snapshot = f.facts->>'service_name',
                   resource_name_snapshot = f.facts->>'resource_name',
                   duration_minutes_snapshot = CASE
                       WHEN pg_input_is_valid(f.facts->>'duration_minutes', 'integer')
                       THEN (f.facts->>'duration_minutes')::integer END,
                   buffer_before_minutes_snapshot = CASE
                       WHEN pg_input_is_valid(f.facts->>'buffer_before_minutes', 'integer')
                       THEN (f.facts->>'buffer_before_minutes')::integer END,
                   buffer_after_minutes_snapshot = CASE
                       WHEN pg_input_is_valid(f.facts->>'buffer_after_minutes', 'integer')
                       THEN (f.facts->>'buffer_after_minutes')::integer END,
                   effective_start_at = CASE
                       WHEN pg_input_is_valid(
                           f.facts->>'effective_start_at', 'timestamp with time zone')
                       THEN (f.facts->>'effective_start_at')::timestamptz END,
                   effective_end_at = CASE
                       WHEN pg_input_is_valid(
                           f.facts->>'effective_end_at', 'timestamp with time zone')
                       THEN (f.facts->>'effective_end_at')::timestamptz END,
                   price_snapshot = CASE
                       WHEN f.facts->'price' = 'null'::jsonb THEN NULL
                       WHEN pg_input_is_valid(f.facts->>'price', 'numeric')
                       THEN (f.facts->>'price')::numeric END,
                   business_timezone_snapshot = f.facts->>'business_timezone',
                   source = 'customer_conversation', version = 1, updated_at = a.created_at
               FROM pending_actions p
               CROSS JOIN LATERAL (
                   SELECT p.proposed_payload->'data'->'facts' AS facts
               ) f
               WHERE p.id = a.pending_action_id"""
        )
    )


def _backfill_allocations() -> None:
    op.get_bind().execute(
        sa.text(
            """INSERT INTO resource_allocations
                   (business_id, resource_id, appointment_id, pending_action_id,
                    allocation_type, status, source, effective_start_at, effective_end_at,
                    idempotency_key, version, created_at, updated_at)
               SELECT business_id, resource_id, id, pending_action_id,
                      'appointment', 'active', source,
                      effective_start_at, effective_end_at,
                      'legacy-appointment-' || id::text, 1,
                      created_at, created_at
               FROM appointments WHERE status IN ('confirmed', 'completed', 'no_show')"""
        )
    )


def upgrade() -> None:
    # Alembic's PostgreSQL migration transaction retains these locks through all
    # preflight, backfill, and DDL below; never move a query above this call.
    _lock_affected_tables()
    if context.is_offline_mode():
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM appointments) THEN "
            "RAISE EXCEPTION 'Migration 0004 requires online appointment preflight'; "
            "END IF; END $$"
        )
    else:
        _preflight_upgrade()

    op.add_column(
        "businesses",
        sa.Column(
            "appointment_booking_horizon_days",
            sa.Integer(),
            server_default="90",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "appointment_minimum_notice_minutes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "appointment_slot_interval_minutes",
            sa.Integer(),
            server_default="15",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_businesses_appointment_horizon",
        "businesses",
        "appointment_booking_horizon_days >= 1 AND appointment_booking_horizon_days <= 365",
    )
    op.create_check_constraint(
        "ck_businesses_appointment_notice",
        "businesses",
        "appointment_minimum_notice_minutes >= 0 AND appointment_minimum_notice_minutes <= 10080",
    )
    op.create_check_constraint(
        "ck_businesses_appointment_slot_interval",
        "businesses",
        "appointment_slot_interval_minutes >= 5 AND appointment_slot_interval_minutes <= 120",
    )
    op.create_check_constraint(
        "ck_businesses_timezone_not_special",
        "businesses",
        "timezone NOT IN ('Factory', 'localtime', 'posixrules') "
        "AND timezone NOT LIKE 'posix/%' AND timezone NOT LIKE 'right/%'",
    )
    op.execute(
        """CREATE FUNCTION validate_business_timezone()
           RETURNS trigger LANGUAGE plpgsql STABLE AS $$
           BEGIN
               IF NOT EXISTS (
                   SELECT 1 FROM pg_timezone_names
                   WHERE name = NEW.timezone
               ) THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_businesses_timezone_recognized',
                       MESSAGE = 'business timezone must be a recognized '
                                 'IANA timezone';
               END IF;
               RETURN NEW;
           END;
           $$"""
    )
    op.execute(
        """CREATE TRIGGER ck_businesses_timezone_recognized
           BEFORE INSERT OR UPDATE OF timezone ON businesses
           FOR EACH ROW EXECUTE FUNCTION validate_business_timezone()"""
    )
    op.drop_constraint("appointment_status", "appointments", type_="check")
    op.create_check_constraint(
        "appointment_status",
        "appointments",
        "status IN ('confirmed', 'completed', 'cancelled', 'no_show')",
    )
    op.drop_column("appointments", "held_until")

    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.add_column(
        "services",
        sa.Column("buffer_before_minutes", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "services",
        sa.Column("buffer_after_minutes", sa.Integer(), server_default="0", nullable=False),
    )
    op.drop_constraint("ck_service_duration", "services", type_="check")
    op.create_check_constraint(
        "ck_service_duration", "services", "duration_minutes > 0 AND duration_minutes <= 720"
    )
    op.create_check_constraint(
        "ck_service_buffer_before",
        "services",
        "buffer_before_minutes >= 0 AND buffer_before_minutes <= 240",
    )
    op.create_check_constraint(
        "ck_service_buffer_after",
        "services",
        "buffer_after_minutes >= 0 AND buffer_after_minutes <= 240",
    )
    op.create_unique_constraint("uq_services_business_id_id", "services", ["business_id", "id"])
    op.create_unique_constraint("uq_resources_business_id_id", "resources", ["business_id", "id"])
    op.create_unique_constraint(
        "uq_appointments_business_id_id", "appointments", ["business_id", "id"]
    )
    op.create_unique_constraint(
        "uq_pending_actions_business_id_id", "pending_actions", ["business_id", "id"]
    )
    op.create_unique_constraint("uq_calls_business_id_id", "calls", ["business_id", "id"])

    op.add_column("operating_schedules", sa.Column("resource_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_operating_schedule_business_resource",
        "operating_schedules",
        "resources",
        ["business_id", "resource_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "operating_schedules_business_id_day_of_week_open_time_key",
        "operating_schedules",
        type_="unique",
    )
    op.create_check_constraint(
        "ck_schedule_time_order", "operating_schedules", "close_time > open_time"
    )
    op.create_index(
        "uq_schedule_business_scope",
        "operating_schedules",
        ["business_id", "day_of_week", "open_time"],
        unique=True,
        postgresql_where=sa.text("resource_id IS NULL"),
    )
    op.create_index(
        "uq_schedule_resource_scope",
        "operating_schedules",
        ["business_id", "resource_id", "day_of_week", "open_time"],
        unique=True,
        postgresql_where=sa.text("resource_id IS NOT NULL"),
    )

    op.add_column("schedule_exceptions", sa.Column("resource_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_schedule_exception_business_resource",
        "schedule_exceptions",
        "resources",
        ["business_id", "resource_id"],
        ["business_id", "id"],
    )
    op.drop_constraint(
        "schedule_exceptions_business_id_exception_date_key", "schedule_exceptions", type_="unique"
    )
    op.create_check_constraint(
        "ck_schedule_exception_consistency",
        "schedule_exceptions",
        "(is_closed AND open_time IS NULL AND close_time IS NULL) OR "
        "(NOT is_closed AND open_time IS NOT NULL AND close_time IS NOT NULL "
        "AND close_time > open_time)",
    )
    op.create_index(
        "uq_exception_business_scope",
        "schedule_exceptions",
        ["business_id", "exception_date"],
        unique=True,
        postgresql_where=sa.text("resource_id IS NULL"),
    )
    op.create_index(
        "uq_exception_resource_scope",
        "schedule_exceptions",
        ["business_id", "resource_id", "exception_date"],
        unique=True,
        postgresql_where=sa.text("resource_id IS NOT NULL"),
    )

    op.create_table(
        "service_resource_eligibility",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", "resource_id", name="uq_service_resource_eligibility"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(
            ["business_id", "service_id"],
            ["services.business_id", "services.id"],
            name="fk_eligibility_business_service",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_eligibility_business_resource",
        ),
    )
    op.create_index(
        "ix_eligibility_business_service_active",
        "service_resource_eligibility",
        ["business_id", "service_id", "is_active"],
    )
    op.create_index(
        "ix_eligibility_business_resource_active",
        "service_resource_eligibility",
        ["business_id", "resource_id", "is_active"],
    )

    for column in (
        sa.Column("effective_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("service_name_snapshot", sa.String(200), nullable=True),
        sa.Column("resource_name_snapshot", sa.String(200), nullable=True),
        sa.Column("duration_minutes_snapshot", sa.Integer(), nullable=True),
        sa.Column("buffer_before_minutes_snapshot", sa.Integer(), nullable=True),
        sa.Column("buffer_after_minutes_snapshot", sa.Integer(), nullable=True),
        sa.Column("price_snapshot", sa.Numeric(10, 2), nullable=True),
        sa.Column("business_timezone_snapshot", sa.String(50), nullable=True),
        sa.Column("source", sa.String(21), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rescheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
        ),
    ):
        op.add_column("appointments", column)

    op.drop_constraint("appointments_service_id_fkey", "appointments", type_="foreignkey")
    op.drop_constraint("appointments_resource_id_fkey", "appointments", type_="foreignkey")
    op.drop_constraint("appointments_pending_action_id_fkey", "appointments", type_="foreignkey")
    op.drop_constraint("appointments_call_id_fkey", "appointments", type_="foreignkey")
    op.create_foreign_key(
        "fk_appointment_business_service",
        "appointments",
        "services",
        ["business_id", "service_id"],
        ["business_id", "id"],
    )
    op.create_foreign_key(
        "fk_appointment_business_resource",
        "appointments",
        "resources",
        ["business_id", "resource_id"],
        ["business_id", "id"],
    )
    op.create_foreign_key(
        "fk_appointment_business_pending_action",
        "appointments",
        "pending_actions",
        ["business_id", "pending_action_id"],
        ["business_id", "id"],
    )
    op.create_foreign_key(
        "fk_appointment_business_call",
        "appointments",
        "calls",
        ["business_id", "call_id"],
        ["business_id", "id"],
    )

    if not context.is_offline_mode():
        _backfill_appointment_facts()
    for name in (
        "service_id",
        "effective_start_at",
        "effective_end_at",
        "service_name_snapshot",
        "resource_name_snapshot",
        "duration_minutes_snapshot",
        "buffer_before_minutes_snapshot",
        "buffer_after_minutes_snapshot",
        "business_timezone_snapshot",
        "source",
        "updated_at",
    ):
        op.alter_column("appointments", name, nullable=False)

    op.create_check_constraint(
        "ck_appt_duration_snapshot",
        "appointments",
        "duration_minutes_snapshot > 0 AND duration_minutes_snapshot <= 720",
    )
    op.create_check_constraint(
        "ck_appt_buffer_before_snapshot",
        "appointments",
        "buffer_before_minutes_snapshot >= 0 AND buffer_before_minutes_snapshot <= 240",
    )
    op.create_check_constraint(
        "ck_appt_buffer_after_snapshot",
        "appointments",
        "buffer_after_minutes_snapshot >= 0 AND buffer_after_minutes_snapshot <= 240",
    )
    op.create_check_constraint(
        "ck_appt_effective_time_order", "appointments", "effective_end_at > effective_start_at"
    )
    op.create_check_constraint(
        "ck_appt_duration_arithmetic",
        "appointments",
        "end_at = start_at + make_interval(mins => duration_minutes_snapshot)",
    )
    op.create_check_constraint(
        "ck_appt_effective_arithmetic",
        "appointments",
        "effective_start_at = start_at - "
        "make_interval(mins => buffer_before_minutes_snapshot) AND "
        "effective_end_at = end_at + "
        "make_interval(mins => buffer_after_minutes_snapshot)",
    )
    op.create_check_constraint("ck_appointment_version", "appointments", "version > 0")
    op.create_check_constraint(
        "ck_appointment_source",
        "appointments",
        "source IN ('customer_conversation', 'owner_manual', 'walk_in')",
    )
    op.create_check_constraint(
        "ck_appointment_source_provenance",
        "appointments",
        "(source = 'customer_conversation' AND pending_action_id IS NOT NULL) OR "
        "(source IN ('owner_manual', 'walk_in') AND pending_action_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_appointment_status_cancelled_at",
        "appointments",
        "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR "
        "(status <> 'cancelled' AND cancelled_at IS NULL)",
    )
    op.create_unique_constraint(
        "uq_appt_pending_provenance", "appointments", ["business_id", "id", "pending_action_id"]
    )

    op.create_table(
        "resource_allocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("pending_action_id", sa.Integer(), nullable=True),
        sa.Column(
            "allocation_type",
            _enum(
                ("appointment", "manual_appointment", "walk_in", "owner_block"),
                "resource_allocation_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum(("active", "released", "cancelled"), "resource_allocation_status"),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "source",
            _enum(
                ("customer_conversation", "owner_manual", "walk_in", "owner_block"),
                "resource_allocation_source",
            ),
            nullable=False,
        ),
        sa.Column("effective_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "idempotency_key", name="uq_allocation_idempotency"),
        sa.CheckConstraint(
            "effective_end_at > effective_start_at", name="ck_allocation_time_order"
        ),
        sa.CheckConstraint("version > 0", name="ck_allocation_version"),
        sa.CheckConstraint(
            "(allocation_type = 'appointment' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NOT NULL AND source = 'customer_conversation') OR "
            "(allocation_type = 'manual_appointment' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NULL AND source = 'owner_manual') OR "
            "(allocation_type = 'walk_in' AND appointment_id IS NOT NULL "
            "AND pending_action_id IS NULL AND source = 'walk_in') OR "
            "(allocation_type = 'owner_block' AND appointment_id IS NULL "
            "AND pending_action_id IS NULL AND source = 'owner_block')",
            name="ck_allocation_type_source_link",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(
            ["business_id", "resource_id"],
            ["resources.business_id", "resources.id"],
            name="fk_allocation_business_resource",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name="fk_allocation_business_appointment",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "appointment_id", "pending_action_id"],
            ["appointments.business_id", "appointments.id", "appointments.pending_action_id"],
            name="fk_allocation_appointment_pending_provenance",
        ),
    )
    op.create_index(
        "uq_allocation_active_appointment",
        "resource_allocations",
        ["appointment_id"],
        unique=True,
        postgresql_where=sa.text("appointment_id IS NOT NULL AND status = 'active'"),
    )
    op.create_index(
        "ix_allocation_business_resource_time",
        "resource_allocations",
        ["business_id", "resource_id", "effective_start_at", "effective_end_at"],
    )
    op.create_index(
        "ix_allocation_business_appointment",
        "resource_allocations",
        ["business_id", "appointment_id"],
    )
    if not context.is_offline_mode():
        _backfill_allocations()
    op.create_exclude_constraint(
        "ex_resource_allocations_active_overlap",
        "resource_allocations",
        ("business_id", "="),
        ("resource_id", "="),
        (
            sa.func.tstzrange(sa.column("effective_start_at"), sa.column("effective_end_at"), "[)"),
            "&&",
        ),
        where=sa.text("status = 'active'"),
        using="gist",
    )
    op.execute(
        """CREATE FUNCTION enforce_one_confirmed_appointment_allocation(
               target_business_id integer, target_appointment_id integer)
           RETURNS void LANGUAGE plpgsql AS $$
           DECLARE
               appointment_status text;
               active_allocations integer;
               matching_active_allocations integer;
           BEGIN
               IF target_appointment_id IS NULL THEN
                   RETURN;
               END IF;
               SELECT status INTO appointment_status
               FROM appointments
               WHERE business_id = target_business_id AND id = target_appointment_id;
               SELECT count(*), count(*) FILTER (
                          WHERE ra.resource_id = a.resource_id
                            AND ra.effective_start_at = a.effective_start_at
                            AND ra.effective_end_at = a.effective_end_at
                            AND ra.source = a.source)
                 INTO active_allocations, matching_active_allocations
               FROM resource_allocations ra
               JOIN appointments a
                 ON a.business_id = ra.business_id AND a.id = ra.appointment_id
               WHERE ra.business_id = target_business_id
                 AND ra.appointment_id = target_appointment_id
                 AND ra.status = 'active';
               IF appointment_status IN ('confirmed', 'completed', 'no_show')
                  AND (active_allocations <> 1 OR matching_active_allocations <> 1) THEN
                   RAISE EXCEPTION USING
                       ERRCODE = '23514',
                       CONSTRAINT = 'ck_confirmed_appointment_active_allocation',
                       MESSAGE = format(
                           '%s appointment requires exactly one '
                           'matching active allocation',
                           appointment_status);
               ELSIF appointment_status = 'cancelled'
                     AND active_allocations <> 0 THEN
                   RAISE EXCEPTION USING
                       ERRCODE = '23514',
                       CONSTRAINT = 'ck_cancelled_appointment_no_active_allocation',
                       MESSAGE = 'cancelled appointment requires zero active allocations';
               END IF;
           END;
           $$"""
    )
    op.execute(
        """CREATE FUNCTION enforce_confirmed_appointment_allocation()
           RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE
               target_business_id integer;
               target_appointment_id integer;
               old_business_id integer;
               old_appointment_id integer;
           BEGIN
               IF TG_TABLE_NAME = 'appointments' THEN
                   IF TG_OP = 'DELETE' THEN
                       target_business_id := OLD.business_id;
                       target_appointment_id := OLD.id;
                   ELSE
                       target_business_id := NEW.business_id;
                       target_appointment_id := NEW.id;
                   END IF;
                   IF TG_OP = 'UPDATE' THEN
                       old_business_id := OLD.business_id;
                       old_appointment_id := OLD.id;
                   END IF;
               ELSE
                   IF TG_OP = 'DELETE' THEN
                       target_business_id := OLD.business_id;
                       target_appointment_id := OLD.appointment_id;
                   ELSE
                       target_business_id := NEW.business_id;
                       target_appointment_id := NEW.appointment_id;
                   END IF;
                   IF TG_OP = 'UPDATE' THEN
                       old_business_id := OLD.business_id;
                       old_appointment_id := OLD.appointment_id;
                   END IF;
               END IF;
               IF TG_OP = 'UPDATE'
                  AND (old_business_id, old_appointment_id)
                      IS DISTINCT FROM (target_business_id, target_appointment_id) THEN
                   PERFORM enforce_one_confirmed_appointment_allocation(
                       old_business_id, old_appointment_id);
               END IF;
               IF target_appointment_id IS NULL THEN
                   RETURN NULL;
               END IF;

               PERFORM enforce_one_confirmed_appointment_allocation(
                   target_business_id, target_appointment_id);
               RETURN NULL;
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_confirmed_appointment_active_allocation_from_appointment
           AFTER INSERT OR UPDATE OR DELETE ON appointments
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION enforce_confirmed_appointment_allocation()"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_confirmed_appointment_active_allocation_from_allocation
           AFTER INSERT OR UPDATE OR DELETE ON resource_allocations
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION enforce_confirmed_appointment_allocation()"""
    )
    op.execute(
        """CREATE FUNCTION appointment_payload_facts_match(
               facts jsonb, a appointments)
           RETURNS boolean LANGUAGE plpgsql STABLE AS $$
           BEGIN
               IF facts IS NULL OR jsonb_typeof(facts) <> 'object' THEN
                   RETURN FALSE;
               END IF;
               IF NOT (facts ? 'service_id' AND facts ? 'resource_id'
                   AND facts ? 'start_at' AND facts ? 'end_at'
                   AND facts ? 'effective_start_at'
                   AND facts ? 'effective_end_at'
                   AND facts ? 'duration_minutes'
                   AND facts ? 'service_name' AND facts ? 'resource_name'
                   AND facts ? 'business_timezone') THEN
                   RETURN FALSE;
               END IF;
               IF NOT COALESCE(
                      pg_input_is_valid(facts->>'service_id', 'bigint'), FALSE)
                  OR NOT COALESCE(
                      pg_input_is_valid(facts->>'resource_id', 'bigint'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                      facts->>'start_at', 'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                      facts->>'end_at', 'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                      facts->>'effective_start_at',
                      'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                      facts->>'effective_end_at',
                      'timestamp with time zone'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(
                      facts->>'duration_minutes', 'integer'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(COALESCE(
                      facts->>'buffer_before_minutes', '0'), 'integer'), FALSE)
                  OR NOT COALESCE(pg_input_is_valid(COALESCE(
                      facts->>'buffer_after_minutes', '0'), 'integer'), FALSE)
               THEN
                   RETURN FALSE;
               END IF;
               RETURN COALESCE(
                   (facts->>'service_id')::bigint = a.service_id
                   AND facts->>'service_name' = a.service_name_snapshot
                   AND (facts->>'resource_id')::bigint = a.resource_id
                   AND facts->>'resource_name' = a.resource_name_snapshot
                   AND (facts->>'start_at')::timestamptz = a.start_at
                   AND (facts->>'end_at')::timestamptz = a.end_at
                   AND (facts->>'effective_start_at')::timestamptz
                       = a.effective_start_at
                   AND (facts->>'effective_end_at')::timestamptz
                       = a.effective_end_at
                   AND (facts->>'duration_minutes')::int
                       = a.duration_minutes_snapshot
                   AND COALESCE(
                       (facts->>'buffer_before_minutes')::int, 0)
                       = a.buffer_before_minutes_snapshot
                   AND COALESCE(
                       (facts->>'buffer_after_minutes')::int, 0)
                       = a.buffer_after_minutes_snapshot
                   AND CASE
                       WHEN facts->'price' IS NULL
                           OR facts->'price' = 'null'::jsonb
                       THEN a.price_snapshot IS NULL
                       WHEN COALESCE(
                           pg_input_is_valid(facts->>'price', 'numeric'), FALSE)
                       THEN (facts->>'price')::numeric = a.price_snapshot
                       ELSE FALSE END
                   AND facts->>'business_timezone'
                       = a.business_timezone_snapshot,
                   FALSE);
           END;
           $$"""
    )
    op.execute(
        r"""CREATE FUNCTION appointment_payload_call_id_matches(
               payload_data jsonb, expected_call_id bigint)
           RETURNS boolean LANGUAGE plpgsql STABLE AS $$
           DECLARE
               call_id_value jsonb;
               call_id_text text;
               parsed_call_id bigint;
           BEGIN
               IF payload_data IS NULL
                  OR jsonb_typeof(payload_data) IS DISTINCT FROM 'object' THEN
                   RETURN FALSE;
               END IF;
               IF NOT (payload_data ? 'call_id') THEN
                   RETURN FALSE;
               END IF;
               call_id_value := payload_data->'call_id';
               IF call_id_value = 'null'::jsonb THEN
                   RETURN expected_call_id IS NULL;
               END IF;
               IF jsonb_typeof(call_id_value) IS DISTINCT FROM 'number' THEN
                   RETURN FALSE;
               END IF;
               call_id_text := payload_data->>'call_id';
               IF call_id_text IS NULL OR call_id_text !~ '^[1-9][0-9]*$' THEN
                   RETURN FALSE;
               END IF;
               IF NOT COALESCE(pg_input_is_valid(call_id_text, 'bigint'), FALSE) THEN
                   RETURN FALSE;
               END IF;
               parsed_call_id := call_id_text::bigint;
               IF parsed_call_id < 1 OR parsed_call_id > 2147483647 THEN
                   RETURN FALSE;
               END IF;
               RETURN COALESCE(parsed_call_id = expected_call_id, FALSE);
           END;
           $$"""
    )
    op.execute(
        r"""CREATE FUNCTION appointment_payload_positive_integer_matches(
               payload_data jsonb, field_name text, expected_value integer)
           RETURNS boolean LANGUAGE plpgsql STABLE AS $$
           DECLARE
               field_value jsonb;
               field_text text;
               parsed_value bigint;
           BEGIN
               IF payload_data IS NULL
                  OR jsonb_typeof(payload_data) IS DISTINCT FROM 'object'
                  OR field_name IS NULL
                  OR field_name = ''
                  OR expected_value IS NULL THEN
                   RETURN FALSE;
               END IF;
               IF NOT (payload_data ? field_name) THEN
                   RETURN FALSE;
               END IF;
               field_value := payload_data->field_name;
               IF field_value = 'null'::jsonb
                  OR jsonb_typeof(field_value) IS DISTINCT FROM 'number' THEN
                   RETURN FALSE;
               END IF;
               field_text := payload_data->>field_name;
               IF field_text IS NULL OR field_text !~ '^[1-9][0-9]*$' THEN
                   RETURN FALSE;
               END IF;
               IF NOT COALESCE(pg_input_is_valid(field_text, 'bigint'), FALSE) THEN
                   RETURN FALSE;
               END IF;
               parsed_value := field_text::bigint;
               IF parsed_value < 1 OR parsed_value > 2147483647 THEN
                   RETURN FALSE;
               END IF;
               RETURN COALESCE(parsed_value = expected_value, FALSE);
           END;
           $$"""
    )
    op.execute(
        """CREATE FUNCTION enforce_appointment_provenance()
           RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE
               payload_data jsonb;
           BEGIN
               IF NEW.source <> 'customer_conversation' THEN
                   RETURN NEW;
               END IF;
               IF TG_OP = 'UPDATE' THEN
                   RETURN NEW;
               END IF;
               SELECT proposed_payload->'data' INTO payload_data
               FROM pending_actions
               WHERE business_id = NEW.business_id AND id = NEW.pending_action_id
                 AND action_type = 'appointment' AND payload_schema_version = 1
                 AND status = 'confirmed'
                 AND committed_entity_type = 'appointment'
                 AND committed_entity_id = NEW.id;
               IF payload_data IS NULL
                  OR payload_data->>'operation' IS DISTINCT FROM 'create'
                  OR appointment_payload_facts_match(
                         payload_data->'facts', NEW) IS NOT TRUE
                  OR payload_data->>'customer_phone'
                        IS DISTINCT FROM NEW.customer_phone
                  OR payload_data->>'customer_name'
                        IS DISTINCT FROM NEW.customer_name
                  OR payload_data->>'reason'
                        IS DISTINCT FROM NEW.reason
                  OR appointment_payload_call_id_matches(
                         payload_data, NEW.call_id) IS NOT TRUE THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_customer_conversation_appointment_provenance',
                       MESSAGE = 'customer-conversation appointment requires matching '
                                 'PendingAction provenance';
               END IF;
               RETURN NEW;
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_customer_conversation_appointment_provenance
           AFTER INSERT OR UPDATE ON appointments
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION enforce_appointment_provenance()"""
    )
    op.execute(
        """CREATE FUNCTION prevent_pending_action_provenance_change()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               IF TG_OP = 'DELETE' AND EXISTS (
                   SELECT 1 FROM appointments WHERE pending_action_id = OLD.id
                   UNION ALL
                   SELECT 1 FROM appointment_commits WHERE pending_action_id = OLD.id) THEN
                   RAISE EXCEPTION USING ERRCODE = '23503',
                       MESSAGE = 'cannot delete PendingAction with committed provenance';
               END IF;
               IF TG_OP = 'UPDATE' AND EXISTS (
                   SELECT 1 FROM appointments WHERE pending_action_id = OLD.id
                   UNION ALL
                   SELECT 1 FROM appointment_commits WHERE pending_action_id = OLD.id) THEN
                   IF (NEW.business_id, NEW.action_type, NEW.payload_schema_version,
                       NEW.proposed_payload)
                      IS DISTINCT FROM
                      (OLD.business_id, OLD.action_type, OLD.payload_schema_version,
                       OLD.proposed_payload) THEN
                       RAISE EXCEPTION USING ERRCODE = '23514',
                           MESSAGE = 'committed PendingAction provenance is immutable';
                   END IF;
                   IF OLD.status = 'confirmed'
                      AND (NEW.status, NEW.committed_entity_type,
                           NEW.committed_entity_id)
                          IS DISTINCT FROM
                          (OLD.status, OLD.committed_entity_type,
                           OLD.committed_entity_id) THEN
                       RAISE EXCEPTION USING ERRCODE = '23514',
                           MESSAGE = 'confirmed PendingAction entity binding is immutable';
                   END IF;
               END IF;
               RETURN COALESCE(NEW, OLD);
           END;
           $$"""
    )

    op.create_table(
        "appointment_commits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("pending_action_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column(
            "operation",
            _enum(("cancel", "reschedule"), "appointment_commit_operation"),
            nullable=False,
        ),
        sa.Column("before_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("reason_code", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pending_action_id", name="uq_appt_commit_pending_action"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"]),
        sa.ForeignKeyConstraint(
            ["business_id", "pending_action_id"],
            ["pending_actions.business_id", "pending_actions.id"],
            name="fk_appt_commit_business_pending_action",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "appointment_id"],
            ["appointments.business_id", "appointments.id"],
            name="fk_appt_commit_business_appointment",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before_snapshot) = 'object'",
            name="ck_appt_commit_before_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(after_snapshot) = 'object'",
            name="ck_appt_commit_after_snapshot_object",
        ),
    )
    op.create_index(
        "ix_appt_commit_business_appointment",
        "appointment_commits",
        ["business_id", "appointment_id"],
    )
    op.create_index(
        "ix_appt_commit_business_pending_action",
        "appointment_commits",
        ["business_id", "pending_action_id"],
    )
    op.execute(
        """CREATE TRIGGER ck_committed_pending_action_provenance
           BEFORE UPDATE OR DELETE ON pending_actions
           FOR EACH ROW EXECUTE FUNCTION prevent_pending_action_provenance_change()"""
    )
    op.execute(
        """CREATE FUNCTION canonical_utc_jsonb(ts timestamptz)
           RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
               SELECT CASE WHEN ts IS NULL THEN 'null'::jsonb
                      WHEN date_trunc('second', ts) = ts
                      THEN to_jsonb(to_char(
                          ts AT TIME ZONE 'UTC',
                          'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
                      ELSE to_jsonb(to_char(
                          ts AT TIME ZONE 'UTC',
                          'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
                      END
           $$"""
    )
    op.execute(
        """CREATE FUNCTION appointment_authoritative_snapshot(a appointments)
           RETURNS jsonb LANGUAGE sql STABLE AS $$
               SELECT jsonb_build_object(
                   'appointment_id', a.id,
                   'business_id', a.business_id,
                   'service_id', a.service_id,
                   'service_name', a.service_name_snapshot,
                   'resource_id', a.resource_id,
                   'resource_name', a.resource_name_snapshot,
                   'customer_name', a.customer_name,
                   'customer_phone', a.customer_phone,
                   'start_at', canonical_utc_jsonb(a.start_at),
                   'end_at', canonical_utc_jsonb(a.end_at),
                   'effective_start_at',
                       canonical_utc_jsonb(a.effective_start_at),
                   'effective_end_at',
                       canonical_utc_jsonb(a.effective_end_at),
                   'duration_minutes', a.duration_minutes_snapshot,
                   'buffer_before_minutes', a.buffer_before_minutes_snapshot,
                   'buffer_after_minutes', a.buffer_after_minutes_snapshot,
                   'price', CASE WHEN a.price_snapshot IS NULL
                       THEN 'null'::jsonb
                       ELSE to_jsonb(a.price_snapshot::text) END,
                   'business_timezone', to_jsonb(a.business_timezone_snapshot),
                   'reason', a.reason,
                   'status', a.status,
                   'source', a.source,
                   'idempotency_key', a.idempotency_key,
                   'pending_action_id', a.pending_action_id,
                   'call_id', a.call_id,
                   'version', a.version,
                   'cancelled_at', canonical_utc_jsonb(a.cancelled_at),
                   'rescheduled_at',
                       canonical_utc_jsonb(a.rescheduled_at),
                   'created_at', canonical_utc_jsonb(a.created_at),
                   'updated_at', canonical_utc_jsonb(a.updated_at))
           $$"""
    )
    op.execute(
        """CREATE FUNCTION enforce_appointment_commit_provenance()
           RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE
               pending_data jsonb;
               current_appointment appointments%ROWTYPE;
           BEGIN
               SELECT proposed_payload->'data' INTO pending_data
               FROM pending_actions
               WHERE business_id = NEW.business_id AND id = NEW.pending_action_id
                 AND action_type = 'appointment' AND payload_schema_version = 1
                 AND status = 'confirmed'
                 AND committed_entity_type = 'appointment_commit'
                 AND committed_entity_id = NEW.id;
               SELECT * INTO current_appointment FROM appointments
               WHERE business_id = NEW.business_id AND id = NEW.appointment_id;
               IF pending_data IS NULL
                  OR pending_data->>'operation' IS DISTINCT FROM NEW.operation::text
                  OR appointment_payload_positive_integer_matches(
                         pending_data,
                         'target_appointment_id',
                         NEW.appointment_id
                     ) IS NOT TRUE
                  OR appointment_payload_positive_integer_matches(
                         pending_data,
                         'target_expected_version',
                         current_appointment.version - 1
                     ) IS NOT TRUE
                  OR (NEW.operation = 'cancel'
                      AND NEW.reason_code
                          IS DISTINCT FROM pending_data->>'reason_code') THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_commit_provenance',
                       MESSAGE = 'appointment commit does not match confirmed PendingAction';
               END IF;
               RETURN NEW;
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_appointment_commit_provenance
           AFTER INSERT ON appointment_commits
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION enforce_appointment_commit_provenance()"""
    )
    op.execute(
        """CREATE FUNCTION enforce_appointment_mutation_commit()
           RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE
               matching_commits integer;
               pending_data jsonb;
               commit_before jsonb;
               commit_after jsonb;
               commit_operation text;
           BEGIN
               IF TG_OP = 'DELETE' THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_mutation_commit',
                       MESSAGE = 'appointments cannot be deleted';
               END IF;
               IF NEW IS NOT DISTINCT FROM OLD THEN
                   RETURN NULL;
               END IF;
               IF OLD.status <> 'confirmed' THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_mutation_commit',
                       MESSAGE = 'terminal appointments are immutable';
               END IF;
               IF NEW.version <> OLD.version + 1
                  OR NEW.updated_at < OLD.updated_at
                  OR (NEW.id, NEW.business_id, NEW.customer_name, NEW.customer_phone,
                      NEW.source, NEW.idempotency_key, NEW.pending_action_id, NEW.call_id,
                      NEW.reason, NEW.created_at)
                     IS DISTINCT FROM
                     (OLD.id, OLD.business_id, OLD.customer_name, OLD.customer_phone,
                      OLD.source, OLD.idempotency_key, OLD.pending_action_id, OLD.call_id,
                      OLD.reason, OLD.created_at) THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_mutation_commit',
                       MESSAGE = 'unsupported appointment mutation';
               END IF;
               IF NEW.status IN ('completed', 'no_show') THEN
                   IF (NEW.service_id, NEW.resource_id, NEW.start_at, NEW.end_at,
                       NEW.effective_start_at, NEW.effective_end_at,
                       NEW.service_name_snapshot, NEW.resource_name_snapshot,
                       NEW.duration_minutes_snapshot, NEW.buffer_before_minutes_snapshot,
                       NEW.buffer_after_minutes_snapshot, NEW.reason, NEW.cancelled_at,
                       NEW.rescheduled_at)
                      IS DISTINCT FROM
                      (OLD.service_id, OLD.resource_id, OLD.start_at, OLD.end_at,
                       OLD.effective_start_at, OLD.effective_end_at,
                       OLD.service_name_snapshot, OLD.resource_name_snapshot,
                       OLD.duration_minutes_snapshot, OLD.buffer_before_minutes_snapshot,
                       OLD.buffer_after_minutes_snapshot, OLD.reason, OLD.cancelled_at,
                       OLD.rescheduled_at) THEN
                       RAISE EXCEPTION USING ERRCODE = '23514',
                           CONSTRAINT = 'ck_appointment_mutation_commit',
                           MESSAGE = 'completion and no-show permit status-only mutation';
                   END IF;
                   RETURN NULL;
               ELSIF NEW.status = 'cancelled' THEN
                   commit_operation := 'cancel';
                   IF NEW.cancelled_at IS NULL
                      OR NEW.rescheduled_at IS DISTINCT FROM OLD.rescheduled_at
                      OR (NEW.service_id, NEW.resource_id, NEW.start_at, NEW.end_at,
                          NEW.effective_start_at, NEW.effective_end_at,
                          NEW.service_name_snapshot, NEW.resource_name_snapshot,
                          NEW.duration_minutes_snapshot, NEW.buffer_before_minutes_snapshot,
                          NEW.buffer_after_minutes_snapshot)
                         IS DISTINCT FROM
                         (OLD.service_id, OLD.resource_id, OLD.start_at, OLD.end_at,
                          OLD.effective_start_at, OLD.effective_end_at,
                          OLD.service_name_snapshot, OLD.resource_name_snapshot,
                          OLD.duration_minutes_snapshot, OLD.buffer_before_minutes_snapshot,
                          OLD.buffer_after_minutes_snapshot) THEN
                       RAISE EXCEPTION USING ERRCODE = '23514',
                           CONSTRAINT = 'ck_appointment_mutation_commit',
                           MESSAGE = 'cancel cannot rewrite appointment facts';
                   END IF;
               ELSIF NEW.status = 'confirmed' THEN
                   commit_operation := 'reschedule';
                   IF NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at
                      OR NEW.rescheduled_at IS NULL
                      OR (NEW.service_id, NEW.resource_id, NEW.start_at, NEW.end_at,
                          NEW.effective_start_at, NEW.effective_end_at,
                          NEW.service_name_snapshot, NEW.resource_name_snapshot,
                          NEW.duration_minutes_snapshot, NEW.buffer_before_minutes_snapshot,
                          NEW.buffer_after_minutes_snapshot)
                         IS NOT DISTINCT FROM
                         (OLD.service_id, OLD.resource_id, OLD.start_at, OLD.end_at,
                          OLD.effective_start_at, OLD.effective_end_at,
                          OLD.service_name_snapshot, OLD.resource_name_snapshot,
                          OLD.duration_minutes_snapshot, OLD.buffer_before_minutes_snapshot,
                          OLD.buffer_after_minutes_snapshot) THEN
                       RAISE EXCEPTION USING ERRCODE = '23514',
                           CONSTRAINT = 'ck_appointment_mutation_commit',
                           MESSAGE = 'reschedule requires an in-place fact change';
                   END IF;
               ELSE
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_mutation_commit',
                       MESSAGE = 'unsupported appointment status transition';
               END IF;
               SELECT count(*), (array_agg(pa.proposed_payload->'data'))[1],
                      (array_agg(ac.before_snapshot))[1], (array_agg(ac.after_snapshot))[1]
                 INTO matching_commits, pending_data, commit_before, commit_after
               FROM appointment_commits ac
               JOIN pending_actions pa
                 ON pa.business_id = ac.business_id AND pa.id = ac.pending_action_id
               WHERE ac.business_id = NEW.business_id
                 AND ac.appointment_id = NEW.id
                 AND ac.operation::text = commit_operation
                 AND pa.status = 'confirmed'
                 AND pa.action_type = 'appointment'
                 AND pa.payload_schema_version = 1
                 AND pa.committed_entity_type = 'appointment_commit'
                 AND pa.committed_entity_id = ac.id
                 AND pa.proposed_payload->'data'->>'operation' = commit_operation
                 AND appointment_payload_positive_integer_matches(
                         pa.proposed_payload->'data',
                         'target_appointment_id',
                         NEW.id)
                 AND appointment_payload_positive_integer_matches(
                         pa.proposed_payload->'data',
                         'target_expected_version',
                         OLD.version);
               IF matching_commits <> 1
                  OR commit_before IS DISTINCT FROM appointment_authoritative_snapshot(OLD)
                  OR commit_after IS DISTINCT FROM appointment_authoritative_snapshot(NEW)
                  OR (commit_operation = 'cancel'
                      AND appointment_payload_facts_match(
                          pending_data->'current_facts', OLD
                      ) IS NOT TRUE)
                  OR (
                      commit_operation = 'reschedule'
                      AND (
                          appointment_payload_facts_match(
                              pending_data->'old_facts', OLD
                          ) IS NOT TRUE
                          OR appointment_payload_facts_match(
                              pending_data->'new_facts', NEW
                          ) IS NOT TRUE
                      )
                  ) THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_mutation_commit',
                       MESSAGE = 'mutation requires one matching immutable commit';
               END IF;
               RETURN NULL;
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_appointment_mutation_commit
           AFTER UPDATE OR DELETE ON appointments
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION enforce_appointment_mutation_commit()"""
    )
    op.execute(
        """CREATE FUNCTION reject_premature_terminal_appointment()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               IF NEW.status IN ('completed', 'no_show')
                  AND NEW.effective_end_at > now() THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_appointment_not_premature_terminal',
                       MESSAGE = 'cannot mark completed or no-show '
                                 'before effective interval ends';
               END IF;
               RETURN NULL;
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_appointment_not_premature_terminal
           AFTER INSERT OR UPDATE ON appointments
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION reject_premature_terminal_appointment()"""
    )
    op.execute(
        """CREATE FUNCTION enforce_confirmed_appointment_action_commit()
           RETURNS trigger LANGUAGE plpgsql AS $$
           DECLARE
               pending_operation text;
               matching_commits integer;
           BEGIN
               pending_operation := NEW.proposed_payload->'data'->>'operation';
               IF NEW.action_type = 'appointment' AND NEW.status = 'confirmed'
                  AND pending_operation IN ('cancel', 'reschedule') THEN
                   SELECT count(*) INTO matching_commits
                   FROM appointment_commits ac
                   WHERE ac.business_id = NEW.business_id
                     AND ac.pending_action_id = NEW.id
                     AND ac.operation::text = pending_operation
                     AND ac.id = NEW.committed_entity_id
                     AND NEW.committed_entity_type = 'appointment_commit';
                   IF matching_commits <> 1 THEN
                       RAISE EXCEPTION USING ERRCODE = '23514',
                           CONSTRAINT = 'ck_confirmed_appointment_action_commit',
                           MESSAGE = 'confirmed mutation requires its appointment commit';
                   END IF;
               END IF;
               RETURN NULL;
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_confirmed_appointment_action_commit
           AFTER INSERT OR UPDATE ON pending_actions
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION enforce_confirmed_appointment_action_commit()"""
    )
    op.execute(
        """CREATE FUNCTION reject_appointment_commit_mutation()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               RAISE EXCEPTION USING ERRCODE = '23514',
                   CONSTRAINT = 'ck_appointment_commit_append_only',
                   MESSAGE = 'appointment commits are append-only';
           END;
           $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER ck_appointment_commit_append_only
           AFTER UPDATE OR DELETE ON appointment_commits
           DEFERRABLE INITIALLY IMMEDIATE
           FOR EACH ROW EXECUTE FUNCTION reject_appointment_commit_mutation()"""
    )
    op.execute(
        """CREATE FUNCTION enforce_resource_allocation_mutation()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               IF TG_OP = 'DELETE' THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_resource_allocation_immutable_identity',
                       MESSAGE = 'resource allocations cannot be deleted';
               END IF;
               IF (NEW.business_id, NEW.appointment_id, NEW.pending_action_id,
                   NEW.allocation_type, NEW.source, NEW.idempotency_key,
                   NEW.resource_id, NEW.effective_start_at, NEW.effective_end_at,
                   NEW.created_at)
                  IS DISTINCT FROM
                  (OLD.business_id, OLD.appointment_id, OLD.pending_action_id,
                   OLD.allocation_type, OLD.source, OLD.idempotency_key,
                   OLD.resource_id, OLD.effective_start_at, OLD.effective_end_at,
                   OLD.created_at)
                  OR OLD.status <> 'active'
                  OR NEW.status NOT IN ('active', 'released', 'cancelled')
                  OR NEW.version <> OLD.version + 1
                  OR NEW.updated_at < OLD.updated_at THEN
                   RAISE EXCEPTION USING ERRCODE = '23514',
                       CONSTRAINT = 'ck_resource_allocation_immutable_identity',
                       MESSAGE = 'illegal resource allocation mutation';
               END IF;
               RETURN NEW;
           END;
           $$"""
    )
    op.execute(
        """CREATE TRIGGER ck_resource_allocation_immutable_identity
           BEFORE UPDATE OR DELETE ON resource_allocations
           FOR EACH ROW EXECUTE FUNCTION enforce_resource_allocation_mutation()"""
    )


def _preflight_downgrade() -> None:
    checks = (
        (
            "SELECT 1 FROM operating_schedules WHERE resource_id IS NOT NULL LIMIT 1",
            "Migration 0004 downgrade cannot preserve resource-specific schedules",
        ),
        (
            "SELECT 1 FROM schedule_exceptions WHERE resource_id IS NOT NULL LIMIT 1",
            "Migration 0004 downgrade cannot preserve resource-specific exceptions",
        ),
        (
            "SELECT 1 FROM service_resource_eligibility LIMIT 1",
            "Migration 0004 downgrade cannot preserve service-resource eligibility",
        ),
        (
            "SELECT 1 FROM appointment_commits LIMIT 1",
            "Migration 0004 downgrade cannot preserve appointment mutation provenance",
        ),
        (
            """SELECT 1 FROM services
               WHERE buffer_before_minutes IS DISTINCT FROM 0
                  OR buffer_after_minutes IS DISTINCT FROM 0 LIMIT 1""",
            "Migration 0004 downgrade cannot preserve service buffers",
        ),
        (
            """SELECT 1 FROM appointments a
               LEFT JOIN services s ON s.id = a.service_id
               LEFT JOIN resources r ON r.id = a.resource_id
               WHERE s.id IS NULL OR r.id IS NULL
                  OR a.source IS DISTINCT FROM 'customer_conversation'
                  OR a.service_name_snapshot IS DISTINCT FROM s.name
                  OR a.resource_name_snapshot IS DISTINCT FROM r.name
                  OR a.duration_minutes_snapshot IS DISTINCT FROM s.duration_minutes
                  OR a.buffer_before_minutes_snapshot IS DISTINCT FROM 0
                  OR a.buffer_after_minutes_snapshot IS DISTINCT FROM 0
                  OR a.effective_start_at IS DISTINCT FROM a.start_at
                  OR a.effective_end_at IS DISTINCT FROM a.end_at
                  OR a.version IS DISTINCT FROM 1
                  OR a.cancelled_at IS NOT NULL
                  OR a.rescheduled_at IS NOT NULL
                  OR a.updated_at IS DISTINCT FROM a.created_at
               LIMIT 1""",
            "Migration 0004 downgrade cannot preserve changed appointment facts",
        ),
        (
            """SELECT 1 FROM resource_allocations ra
               LEFT JOIN appointments a
                 ON a.business_id = ra.business_id AND a.id = ra.appointment_id
               WHERE a.id IS NULL
                  OR a.status IS DISTINCT FROM 'confirmed'
                  OR ra.allocation_type IS DISTINCT FROM 'appointment'
                  OR ra.status IS DISTINCT FROM 'active'
                  OR ra.source IS DISTINCT FROM 'customer_conversation'
                  OR ra.pending_action_id IS DISTINCT FROM a.pending_action_id
                  OR ra.resource_id IS DISTINCT FROM a.resource_id
                  OR ra.effective_start_at IS DISTINCT FROM a.start_at
                  OR ra.effective_end_at IS DISTINCT FROM a.end_at
                  OR ra.reason IS NOT NULL
                  OR ra.idempotency_key IS DISTINCT FROM
                        'legacy-appointment-' || a.id::text
                  OR ra.version IS DISTINCT FROM 1
                  OR ra.created_at IS DISTINCT FROM a.created_at
                  OR ra.updated_at IS DISTINCT FROM a.created_at
               LIMIT 1""",
            "Migration 0004 downgrade cannot preserve modified resource allocations",
        ),
        (
            """SELECT 1 FROM appointments a
               LEFT JOIN resource_allocations ra
                 ON ra.business_id = a.business_id
                AND ra.appointment_id = a.id
                AND ra.status = 'active'
               WHERE (a.status = 'confirmed' AND ra.id IS NULL)
                  OR (a.status <> 'confirmed' AND ra.id IS NOT NULL)
               LIMIT 1""",
            "Migration 0004 downgrade requires canonical appointment-allocation correspondence",
        ),
    )
    for sql, message in checks:
        _fail_if_exists(sql, message)


def downgrade() -> None:
    # Use the upgrade lock set and order so rollback/retry paths cannot deadlock.
    # The surrounding migration transaction holds the locks through destruction.
    _lock_affected_tables()
    if context.is_offline_mode():
        op.execute(
            "DO $$ BEGIN RAISE EXCEPTION 'Migration 0004 downgrade requires "
            "online representability preflight'; END $$"
        )
    else:
        _preflight_downgrade()

    op.execute("DROP TRIGGER ck_resource_allocation_immutable_identity ON resource_allocations")
    op.execute("DROP FUNCTION enforce_resource_allocation_mutation()")
    op.execute("DROP TRIGGER ck_appointment_commit_append_only ON appointment_commits")
    op.execute("DROP FUNCTION reject_appointment_commit_mutation()")
    op.execute("DROP TRIGGER ck_confirmed_appointment_action_commit ON pending_actions")
    op.execute("DROP FUNCTION enforce_confirmed_appointment_action_commit()")
    op.execute("DROP TRIGGER ck_appointment_not_premature_terminal ON appointments")
    op.execute("DROP FUNCTION reject_premature_terminal_appointment()")
    op.execute("DROP TRIGGER ck_appointment_mutation_commit ON appointments")
    op.execute("DROP FUNCTION enforce_appointment_mutation_commit()")
    op.execute("DROP TRIGGER ck_appointment_commit_provenance ON appointment_commits")
    op.execute("DROP FUNCTION enforce_appointment_commit_provenance()")
    op.execute("DROP FUNCTION appointment_payload_positive_integer_matches(jsonb, text, integer)")
    op.execute("DROP FUNCTION appointment_authoritative_snapshot(appointments)")
    op.execute("DROP FUNCTION canonical_utc_jsonb(timestamptz)")
    op.execute("DROP TRIGGER ck_committed_pending_action_provenance ON pending_actions")
    op.execute("DROP FUNCTION prevent_pending_action_provenance_change()")
    op.drop_index("ix_appt_commit_business_pending_action", table_name="appointment_commits")
    op.drop_index("ix_appt_commit_business_appointment", table_name="appointment_commits")
    op.drop_table("appointment_commits")
    op.execute("DROP TRIGGER ck_customer_conversation_appointment_provenance ON appointments")
    op.execute("DROP FUNCTION enforce_appointment_provenance()")
    op.execute("DROP FUNCTION appointment_payload_call_id_matches(jsonb, bigint)")
    op.execute("DROP FUNCTION appointment_payload_facts_match(jsonb, appointments)")
    op.execute(
        "DROP TRIGGER ck_confirmed_appointment_active_allocation_from_allocation "
        "ON resource_allocations"
    )
    op.execute(
        "DROP TRIGGER ck_confirmed_appointment_active_allocation_from_appointment ON appointments"
    )
    op.execute("DROP FUNCTION enforce_confirmed_appointment_allocation()")
    op.execute("DROP FUNCTION enforce_one_confirmed_appointment_allocation(integer, integer)")
    op.drop_constraint("appointment_status", "appointments", type_="check")
    op.create_check_constraint(
        "appointment_status",
        "appointments",
        "status IN ('held', 'confirmed', 'completed', 'cancelled', 'no_show')",
    )
    op.add_column(
        "appointments",
        sa.Column("held_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "ALTER TABLE resource_allocations DROP CONSTRAINT ex_resource_allocations_active_overlap"
    )
    op.drop_index("ix_allocation_business_appointment", table_name="resource_allocations")
    op.drop_index("ix_allocation_business_resource_time", table_name="resource_allocations")
    op.drop_index("uq_allocation_active_appointment", table_name="resource_allocations")
    op.drop_table("resource_allocations")
    op.drop_constraint("uq_appt_pending_provenance", "appointments", type_="unique")
    for constraint in (
        "ck_appointment_status_cancelled_at",
        "ck_appointment_source_provenance",
        "ck_appointment_source",
        "ck_appt_effective_arithmetic",
        "ck_appt_duration_arithmetic",
        "ck_appt_effective_time_order",
        "ck_appt_buffer_after_snapshot",
        "ck_appt_buffer_before_snapshot",
        "ck_appt_duration_snapshot",
    ):
        op.drop_constraint(constraint, "appointments", type_="check")
    op.drop_constraint("fk_appointment_business_call", "appointments", type_="foreignkey")
    op.drop_constraint("fk_appointment_business_pending_action", "appointments", type_="foreignkey")
    op.drop_constraint("fk_appointment_business_resource", "appointments", type_="foreignkey")
    op.drop_constraint("fk_appointment_business_service", "appointments", type_="foreignkey")
    op.create_foreign_key(
        "appointments_service_id_fkey", "appointments", "services", ["service_id"], ["id"]
    )
    op.create_foreign_key(
        "appointments_resource_id_fkey", "appointments", "resources", ["resource_id"], ["id"]
    )
    op.create_foreign_key(
        "appointments_pending_action_id_fkey",
        "appointments",
        "pending_actions",
        ["pending_action_id"],
        ["id"],
    )
    op.create_foreign_key("appointments_call_id_fkey", "appointments", "calls", ["call_id"], ["id"])
    op.alter_column("appointments", "service_id", nullable=True)
    for column in (
        "updated_at",
        "rescheduled_at",
        "cancelled_at",
        "version",
        "source",
        "buffer_after_minutes_snapshot",
        "business_timezone_snapshot",
        "price_snapshot",
        "buffer_before_minutes_snapshot",
        "duration_minutes_snapshot",
        "resource_name_snapshot",
        "service_name_snapshot",
        "effective_end_at",
        "effective_start_at",
    ):
        op.drop_column("appointments", column)

    op.drop_index(
        "ix_eligibility_business_resource_active", table_name="service_resource_eligibility"
    )
    op.drop_index(
        "ix_eligibility_business_service_active", table_name="service_resource_eligibility"
    )
    op.drop_table("service_resource_eligibility")
    op.drop_index("uq_exception_resource_scope", table_name="schedule_exceptions")
    op.drop_index("uq_exception_business_scope", table_name="schedule_exceptions")
    op.drop_constraint("ck_schedule_exception_consistency", "schedule_exceptions", type_="check")
    op.drop_constraint(
        "fk_schedule_exception_business_resource", "schedule_exceptions", type_="foreignkey"
    )
    op.drop_column("schedule_exceptions", "resource_id")
    op.create_unique_constraint(
        "schedule_exceptions_business_id_exception_date_key",
        "schedule_exceptions",
        ["business_id", "exception_date"],
    )
    op.drop_index("uq_schedule_resource_scope", table_name="operating_schedules")
    op.drop_index("uq_schedule_business_scope", table_name="operating_schedules")
    op.drop_constraint("ck_schedule_time_order", "operating_schedules", type_="check")
    op.drop_constraint(
        "fk_operating_schedule_business_resource", "operating_schedules", type_="foreignkey"
    )
    op.drop_column("operating_schedules", "resource_id")
    op.create_unique_constraint(
        "operating_schedules_business_id_day_of_week_open_time_key",
        "operating_schedules",
        ["business_id", "day_of_week", "open_time"],
    )
    op.drop_constraint("uq_calls_business_id_id", "calls", type_="unique")
    op.drop_constraint("uq_pending_actions_business_id_id", "pending_actions", type_="unique")
    op.drop_constraint("uq_appointments_business_id_id", "appointments", type_="unique")
    op.drop_constraint("uq_resources_business_id_id", "resources", type_="unique")
    op.drop_constraint("uq_services_business_id_id", "services", type_="unique")
    op.drop_constraint("ck_service_buffer_after", "services", type_="check")
    op.drop_constraint("ck_service_buffer_before", "services", type_="check")
    op.drop_constraint("ck_service_duration", "services", type_="check")
    op.create_check_constraint("ck_service_duration", "services", "duration_minutes > 0")
    op.drop_column("services", "buffer_after_minutes")
    op.drop_column("services", "buffer_before_minutes")
    op.execute("DROP TRIGGER ck_businesses_timezone_recognized ON businesses")
    op.execute("DROP FUNCTION validate_business_timezone()")
    op.drop_constraint("ck_businesses_timezone_not_special", "businesses", type_="check")
    op.drop_constraint("ck_businesses_appointment_slot_interval", "businesses", type_="check")
    op.drop_constraint("ck_businesses_appointment_notice", "businesses", type_="check")
    op.drop_constraint("ck_businesses_appointment_horizon", "businesses", type_="check")
    op.drop_column("businesses", "appointment_slot_interval_minutes")
    op.drop_column("businesses", "appointment_minimum_notice_minutes")
    op.drop_column("businesses", "appointment_booking_horizon_days")
