"""Use PostgreSQL-supported JSONB cardinality in snapshot validation.

Revision ID: a817pg17
Revises: a817pg16
"""

from alembic import op


revision = "a817pg17"
down_revision = "a817pg16"
branch_labels = None
depends_on = None


def _replace_snapshot_validator(*, object_count_expression: str) -> None:
    op.execute(
        f"""
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
               OR {object_count_expression}
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
                       OR (fact.value #>> '{{}}') !~ '^[0-9]+([.][0-9]+)?$'
               )
               OR NEW.total IS DISTINCT FROM (
                   SELECT sum((fact.value #>> '{{}}')::numeric)
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
    )


def upgrade() -> None:
    _replace_snapshot_validator(
        object_count_expression="(SELECT count(*) FROM jsonb_object_keys(NEW.nightly_bases))"
    )


def downgrade() -> None:
    # Never make PG16 writes inoperable during rollback. The previous revision's
    # jsonb_object_length() expression does not exist on PostgreSQL; retaining the
    # supported equivalent is the only safe downgrade for this compatibility fix.
    _replace_snapshot_validator(
        object_count_expression="(SELECT count(*) FROM jsonb_object_keys(NEW.nightly_bases))"
    )
