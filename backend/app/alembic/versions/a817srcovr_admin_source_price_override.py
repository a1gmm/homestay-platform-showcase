"""Add audited administrator source-price override snapshots.

Revision ID: a817srcovr
Revises: a814bypms01
Create Date: 2026-08-17
"""

from alembic import op


revision = "a817srcovr"
down_revision = "a814bypms01"
branch_labels = None
depends_on = None


_VALIDATE_WITH_OVERRIDE = """
CREATE OR REPLACE FUNCTION validate_source_price_snapshot_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_channel channel;
    source_check_in date;
    source_check_out date;
    latest_version integer;
BEGIN
    SELECT o.channel, o.check_in_date, o.check_out_date
      INTO source_channel, source_check_in, source_check_out
      FROM orders AS o
     WHERE o.order_id = NEW.source_order_id
       AND NOT o.is_deleted
     FOR UPDATE;
    IF NOT FOUND
       OR source_channel IS DISTINCT FROM NEW.channel
       OR source_check_in IS DISTINCT FROM NEW.check_in_date
       OR source_check_out IS DISTINCT FROM NEW.check_out_date
    THEN
        RAISE EXCEPTION 'source order and price snapshot must be consistent'
            USING ERRCODE = '23514';
    END IF;

    IF jsonb_typeof(NEW.nightly_bases) <> 'object'
       OR jsonb_object_length(NEW.nightly_bases)
            <> (NEW.check_out_date - NEW.check_in_date)
       OR EXISTS (
           SELECT 1
             FROM generate_series(
                 NEW.check_in_date,
                 NEW.check_out_date - 1,
                 interval '1 day'
             ) AS expected(stay_date)
            WHERE NOT NEW.nightly_bases
                ? to_char(expected.stay_date, 'YYYY-MM-DD')
       )
       OR EXISTS (
           SELECT 1 FROM jsonb_each(NEW.nightly_bases) AS fact
            WHERE jsonb_typeof(fact.value) <> 'string'
               OR (fact.value #>> '{}') !~ '^[0-9]+([.][0-9]+)?$'
       )
       OR NEW.total IS DISTINCT FROM (
           SELECT sum((fact.value #>> '{}')::numeric)
             FROM jsonb_each(NEW.nightly_bases) AS fact
       )
    THEN
        RAISE EXCEPTION 'source snapshot requires exact nightly decimal-string facts'
            USING ERRCODE = '23514';
    END IF;

    SELECT max(s.version) INTO latest_version
      FROM order_source_price_snapshots AS s
     WHERE s.source_order_id = NEW.source_order_id;
    IF NEW.version IS DISTINCT FROM coalesce(latest_version, 0) + 1 THEN
        RAISE EXCEPTION 'source price snapshot version must append sequentially'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.origin::text = 'administrator_fallback'
       AND EXISTS (
           SELECT 1 FROM order_source_price_snapshots AS upstream
           WHERE upstream.source_order_id = NEW.source_order_id
             AND upstream.origin::text IN ('bypms_import', 'bypms_adoption')
       )
    THEN
        RAISE EXCEPTION 'fallback forbidden when a valid upstream snapshot exists'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.origin::text IN ('administrator_fallback', 'administrator_override')
       AND NOT EXISTS (
           SELECT 1
           FROM users AS u
           JOIN audit_logs AS a ON a.operator_id = u.user_id
           WHERE u.user_id = NEW.created_by
             AND u.role = 'admin'
             AND a.log_id = NEW.audit_log_id
             AND a.action = CASE NEW.origin::text
                 WHEN 'administrator_fallback'
                     THEN 'sponsorship.source_price_fallback'
                 ELSE 'sponsorship.source_price_override'
             END
             AND a.resource_type = 'order'
             AND a.resource_id = NEW.source_order_id
       )
    THEN
        RAISE EXCEPTION 'administrator source price requires a matching admin audit log'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$
"""


_VALIDATE_FALLBACK_ONLY = """
CREATE OR REPLACE FUNCTION validate_source_price_snapshot_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    source_channel channel;
    source_check_in date;
    source_check_out date;
    latest_version integer;
BEGIN
    SELECT o.channel, o.check_in_date, o.check_out_date
      INTO source_channel, source_check_in, source_check_out
      FROM orders AS o
     WHERE o.order_id = NEW.source_order_id
       AND NOT o.is_deleted
     FOR UPDATE;
    IF NOT FOUND
       OR source_channel IS DISTINCT FROM NEW.channel
       OR source_check_in IS DISTINCT FROM NEW.check_in_date
       OR source_check_out IS DISTINCT FROM NEW.check_out_date
    THEN
        RAISE EXCEPTION 'source order and price snapshot must be consistent'
            USING ERRCODE = '23514';
    END IF;

    IF jsonb_typeof(NEW.nightly_bases) <> 'object'
       OR jsonb_object_length(NEW.nightly_bases)
            <> (NEW.check_out_date - NEW.check_in_date)
       OR EXISTS (
           SELECT 1
             FROM generate_series(
                 NEW.check_in_date,
                 NEW.check_out_date - 1,
                 interval '1 day'
             ) AS expected(stay_date)
            WHERE NOT NEW.nightly_bases
                ? to_char(expected.stay_date, 'YYYY-MM-DD')
       )
       OR EXISTS (
           SELECT 1 FROM jsonb_each(NEW.nightly_bases) AS fact
            WHERE jsonb_typeof(fact.value) <> 'string'
               OR (fact.value #>> '{}') !~ '^[0-9]+([.][0-9]+)?$'
       )
       OR NEW.total IS DISTINCT FROM (
           SELECT sum((fact.value #>> '{}')::numeric)
             FROM jsonb_each(NEW.nightly_bases) AS fact
       )
    THEN
        RAISE EXCEPTION 'source snapshot requires exact nightly decimal-string facts'
            USING ERRCODE = '23514';
    END IF;

    SELECT max(s.version) INTO latest_version
      FROM order_source_price_snapshots AS s
     WHERE s.source_order_id = NEW.source_order_id;
    IF NEW.version IS DISTINCT FROM coalesce(latest_version, 0) + 1 THEN
        RAISE EXCEPTION 'source price snapshot version must append sequentially'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.origin::text = 'administrator_fallback'
       AND EXISTS (
           SELECT 1 FROM order_source_price_snapshots AS upstream
           WHERE upstream.source_order_id = NEW.source_order_id
             AND upstream.origin::text IN ('bypms_import', 'bypms_adoption')
       )
    THEN
        RAISE EXCEPTION 'fallback forbidden when a valid upstream snapshot exists'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.origin::text = 'administrator_fallback'
       AND NOT EXISTS (
           SELECT 1
           FROM users AS u
           JOIN audit_logs AS a ON a.operator_id = u.user_id
           WHERE u.user_id = NEW.created_by
             AND u.role = 'admin'
             AND a.log_id = NEW.audit_log_id
             AND a.action = 'sponsorship.source_price_fallback'
             AND a.resource_type = 'order'
             AND a.resource_id = NEW.source_order_id
       )
    THEN
        RAISE EXCEPTION 'administrator fallback requires a matching admin audit log'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        "ALTER TYPE source_price_snapshot_origin "
        "ADD VALUE IF NOT EXISTS 'administrator_override'"
    )
    op.drop_constraint(
        "ck_source_price_snapshot_audited_fallback",
        "order_source_price_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_price_snapshot_audited_fallback",
        "order_source_price_snapshots",
        "origin::text NOT IN ('administrator_fallback', 'administrator_override') "
        "OR (created_by IS NOT NULL AND audit_log_id IS NOT NULL)",
    )
    op.execute(_VALIDATE_WITH_OVERRIDE)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM order_source_price_snapshots
                 WHERE origin::text = 'administrator_override'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while administrator_override snapshots exist';
            END IF;
        END;
        $$
        """
    )
    op.execute(_VALIDATE_FALLBACK_ONLY)
    op.drop_constraint(
        "ck_source_price_snapshot_audited_fallback",
        "order_source_price_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_price_snapshot_audited_fallback",
        "order_source_price_snapshots",
        "origin::text <> 'administrator_fallback' "
        "OR (created_by IS NOT NULL AND audit_log_id IS NOT NULL)",
    )
    op.execute(
        "ALTER TYPE source_price_snapshot_origin "
        "RENAME TO source_price_snapshot_origin_with_override"
    )
    op.execute(
        "CREATE TYPE source_price_snapshot_origin AS ENUM "
        "('bypms_import', 'bypms_adoption', 'administrator_fallback')"
    )
    op.execute(
        "ALTER TABLE order_source_price_snapshots ALTER COLUMN origin "
        "TYPE source_price_snapshot_origin "
        "USING origin::text::source_price_snapshot_origin"
    )
    op.execute("DROP TYPE source_price_snapshot_origin_with_override")
