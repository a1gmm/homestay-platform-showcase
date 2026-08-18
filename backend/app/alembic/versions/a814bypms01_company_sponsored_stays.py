"""Add typed sponsored-stay financial facts and immutable source prices.

Revision ID: a814bypms01
Revises: a814syncconf
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a814bypms01"
down_revision = "a814syncconf"
branch_labels = None
depends_on = None


stay_settlement_kind = postgresql.ENUM(
    "free_room", "company_sponsored", name="stay_settlement_kind", create_type=False
)
snapshot_origin = postgresql.ENUM(
    "bypms_import",
    "bypms_adoption",
    "administrator_fallback",
    name="source_price_snapshot_origin",
    create_type=False,
)
payment_responsibility = postgresql.ENUM(
    "company_payable",
    "channel_settled",
    name="sponsorship_payment_responsibility",
    create_type=False,
)
sponsorship_status = postgresql.ENUM(
    "confirmed", "settled", "voided", name="company_sponsorship_status", create_type=False
)
channel = postgresql.ENUM(name="channel", create_type=False)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    bind = op.get_bind()
    stay_settlement_kind.create(bind, checkfirst=True)
    snapshot_origin.create(bind, checkfirst=True)
    payment_responsibility.create(bind, checkfirst=True)
    sponsorship_status.create(bind, checkfirst=True)

    op.add_column(
        "orders",
        sa.Column("stay_settlement_kind", stay_settlement_kind, nullable=True),
    )

    op.create_table(
        "order_source_price_snapshots",
        sa.Column("source_price_snapshot_id", sa.String(40), primary_key=True),
        sa.Column(
            "source_order_id",
            sa.String(20),
            sa.ForeignKey("orders.order_id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("upstream_payload_hash", sa.String(64), nullable=False),
        sa.Column("channel", channel, nullable=False),
        sa.Column("check_in_date", sa.Date(), nullable=False),
        sa.Column("check_out_date", sa.Date(), nullable=False),
        sa.Column("nightly_bases", postgresql.JSONB(), nullable=False),
        sa.Column("total", sa.Numeric(10, 2), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", snapshot_origin, nullable=False),
        sa.Column("created_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("audit_log_id", sa.BigInteger(), sa.ForeignKey("audit_logs.log_id")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("version > 0", name="ck_source_price_snapshot_positive_version"),
        sa.CheckConstraint(
            "check_out_date > check_in_date", name="ck_source_price_snapshot_dates"
        ),
        sa.CheckConstraint(
            "total >= 0", name="ck_source_price_snapshot_nonnegative_total"
        ),
        sa.CheckConstraint(
            "origin <> 'administrator_fallback' "
            "OR (created_by IS NOT NULL AND audit_log_id IS NOT NULL)",
            name="ck_source_price_snapshot_audited_fallback",
        ),
        sa.UniqueConstraint(
            "source_order_id",
            "version",
            name="uq_source_price_snapshots_order_version",
        ),
        sa.UniqueConstraint(
            "source_order_id",
            "upstream_payload_hash",
            "origin",
            name="uq_source_price_snapshots_order_payload_origin",
        ),
    )
    op.create_index(
        "ix_source_price_snapshots_source",
        "order_source_price_snapshots",
        ["source_order_id"],
    )
    op.create_unique_constraint(
        "uq_owner_settlement_items_item_batch",
        "owner_settlement_items",
        ["item_id", "settlement_id"],
    )
    op.add_column(
        "owner_settlement_items",
        sa.Column(
            "externally_settled_income",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "company_sponsored_stays",
        sa.Column("sponsored_stay_id", sa.String(40), primary_key=True),
        sa.Column(
            "source_order_id",
            sa.String(20),
            sa.ForeignKey("orders.order_id"),
            nullable=False,
        ),
        sa.Column(
            "segment_order_id",
            sa.String(20),
            sa.ForeignKey("orders.order_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("segment_check_in_date", sa.Date(), nullable=False),
        sa.Column("segment_check_out_date", sa.Date(), nullable=False),
        sa.Column("channel", channel, nullable=False),
        sa.Column("calculation_base", sa.Numeric(10, 2), nullable=False),
        sa.Column("settlement_ratio", sa.Numeric(5, 4), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "payment_responsibility",
            payment_responsibility,
            nullable=False,
            server_default="company_payable",
        ),
        sa.Column(
            "status", sponsorship_status, nullable=False, server_default="confirmed"
        ),
        sa.Column(
            "source_price_snapshot_id",
            sa.String(40),
            sa.ForeignKey("order_source_price_snapshots.source_price_snapshot_id"),
            nullable=False,
        ),
        sa.Column(
            "settlement_item_id",
            sa.String(20),
        ),
        sa.Column(
            "settlement_batch_id",
            sa.String(20),
            sa.ForeignKey("owner_settlements.settlement_id"),
        ),
        sa.Column("created_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("updated_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "segment_check_out_date > segment_check_in_date", name="ck_sponsored_stay_dates"
        ),
        sa.CheckConstraint(
            "calculation_base >= 0", name="ck_sponsored_stay_nonnegative_base"
        ),
        sa.CheckConstraint(
            "settlement_ratio >= 0 AND settlement_ratio <= 1",
            name="ck_sponsored_stay_ratio_range",
        ),
        sa.CheckConstraint("amount >= 0", name="ck_sponsored_stay_nonnegative_amount"),
        sa.CheckConstraint("version >= 1", name="ck_sponsored_stay_positive_version"),
        sa.CheckConstraint(
            "(settlement_item_id IS NULL AND settlement_batch_id IS NULL) OR "
            "(settlement_item_id IS NOT NULL AND settlement_batch_id IS NOT NULL)",
            name="ck_sponsored_stay_settlement_refs_pair",
        ),
        sa.CheckConstraint(
            "status <> 'settled' OR "
            "(settlement_item_id IS NOT NULL AND settlement_batch_id IS NOT NULL)",
            name="ck_sponsored_stay_settled_refs",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_item_id", "settlement_batch_id"],
            ["owner_settlement_items.item_id", "owner_settlement_items.settlement_id"],
            name="fk_sponsored_stay_settlement_item_batch",
        ),
    )
    op.create_index(
        "ix_company_sponsored_stays_source",
        "company_sponsored_stays",
        ["source_order_id"],
    )
    op.create_index(
        "uq_company_sponsored_stays_active_identity",
        "company_sponsored_stays",
        ["source_order_id", "segment_check_in_date", "segment_check_out_date"],
        unique=True,
        postgresql_where=sa.text("status <> 'voided'"),
    )

    op.create_table(
        "company_sponsorship_adjustments",
        sa.Column("adjustment_id", sa.String(40), primary_key=True),
        sa.Column(
            "sponsorship_id",
            sa.String(40),
            sa.ForeignKey("company_sponsored_stays.sponsored_stay_id"),
            nullable=False,
        ),
        sa.Column("delta", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operation_key", sa.String(255), nullable=False),
        sa.Column("actor_id", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("system_principal", sa.String(80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "delta <> 0", name="ck_sponsorship_adjustment_nonzero_delta"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_sponsorship_adjustment_nonblank_reason",
        ),
        sa.CheckConstraint(
            "length(trim(operation_key)) > 0",
            name="ck_sponsorship_adjustment_nonblank_operation_key",
        ),
        sa.CheckConstraint(
            "actor_id IS NULL OR length(trim(actor_id)) > 0",
            name="ck_sponsorship_adjustment_nonblank_actor",
        ),
        sa.CheckConstraint(
            "system_principal IS NULL OR length(trim(system_principal)) > 0",
            name="ck_sponsorship_adjustment_nonblank_system_principal",
        ),
        sa.CheckConstraint(
            "CAST(delta AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity', 'Inf', '-Inf')",
            name="ck_sponsorship_adjustment_finite_delta",
        ),
        sa.CheckConstraint(
            "(actor_id IS NOT NULL AND system_principal IS NULL) OR "
            "(actor_id IS NULL AND system_principal IS NOT NULL)",
            name="ck_sponsorship_adjustment_one_principal",
        ),
        sa.UniqueConstraint(
            "sponsorship_id",
            "operation_key",
            name="uq_sponsorship_adjustments_root_operation",
        ),
    )
    op.create_index(
        "ix_sponsorship_adjustments_root_created",
        "company_sponsorship_adjustments",
        ["sponsorship_id", "created_at"],
    )
    op.add_column(
        "owner_settlement_items",
        sa.Column("sponsorship_adjustment_id", sa.String(40), nullable=True),
    )
    op.create_foreign_key(
        "fk_owner_settlement_item_sponsorship_adjustment",
        "owner_settlement_items",
        "company_sponsorship_adjustments",
        ["sponsorship_adjustment_id"],
        ["adjustment_id"],
    )
    op.create_unique_constraint(
        "uq_owner_settlement_item_sponsorship_adjustment",
        "owner_settlement_items",
        ["sponsorship_adjustment_id"],
    )

    # ORM guards make local/unit behavior fail fast; these triggers are the final
    # authority for raw SQL, imports and any future writer outside this service.
    op.execute(
        """
        CREATE FUNCTION validate_source_price_snapshot_insert()
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

            IF NEW.origin = 'administrator_fallback'
               AND EXISTS (
                   SELECT 1 FROM order_source_price_snapshots AS upstream
                   WHERE upstream.source_order_id = NEW.source_order_id
                     AND upstream.origin IN ('bypms_import', 'bypms_adoption')
               )
            THEN
                RAISE EXCEPTION 'fallback forbidden when a valid upstream snapshot exists'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.origin = 'administrator_fallback'
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
    )
    op.execute(
        """
        CREATE TRIGGER trg_source_price_snapshot_insert
        BEFORE INSERT ON order_source_price_snapshots
        FOR EACH ROW EXECUTE FUNCTION validate_source_price_snapshot_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_sponsorship_immutable_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000';
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_order_source_price_snapshots_immutable
        BEFORE UPDATE OR DELETE ON order_source_price_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_sponsorship_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_sponsorship_root_fact_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.source_order_id IS DISTINCT FROM NEW.source_order_id
               OR OLD.segment_order_id IS DISTINCT FROM NEW.segment_order_id
               OR OLD.segment_check_in_date IS DISTINCT FROM NEW.segment_check_in_date
               OR OLD.segment_check_out_date IS DISTINCT FROM NEW.segment_check_out_date
               OR OLD.channel IS DISTINCT FROM NEW.channel
               OR OLD.calculation_base IS DISTINCT FROM NEW.calculation_base
               OR OLD.settlement_ratio IS DISTINCT FROM NEW.settlement_ratio
               OR OLD.amount IS DISTINCT FROM NEW.amount
               OR OLD.payment_responsibility IS DISTINCT FROM NEW.payment_responsibility
               OR OLD.source_price_snapshot_id IS DISTINCT FROM NEW.source_price_snapshot_id
            THEN
                RAISE EXCEPTION 'company sponsorship root financial facts are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.version IS DISTINCT FROM OLD.version
               AND NEW.version IS DISTINCT FROM OLD.version + 1
            THEN
                RAISE EXCEPTION 'company sponsorship version must advance by one'
                    USING ERRCODE = '23514';
            END IF;
            IF OLD.status = 'settled' AND NEW.status <> 'settled' THEN
                RAISE EXCEPTION 'settled sponsorship status is immutable; append an adjustment'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status = 'voided' AND NEW.status <> 'voided' THEN
                RAISE EXCEPTION 'voided sponsorship status is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_company_sponsored_stays_immutable_facts
        BEFORE UPDATE ON company_sponsored_stays
        FOR EACH ROW EXECUTE FUNCTION reject_sponsorship_root_fact_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_company_sponsored_stay()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Match the service's deterministic order: both orders by identity,
            -- then the selected immutable snapshot.  This closes validation races.
            PERFORM 1
              FROM orders AS locked_order
             WHERE locked_order.order_id IN (NEW.source_order_id, NEW.segment_order_id)
             ORDER BY locked_order.order_id
             FOR UPDATE;
            PERFORM 1
              FROM order_source_price_snapshots AS locked_snapshot
             WHERE locked_snapshot.source_price_snapshot_id = NEW.source_price_snapshot_id
             FOR UPDATE;

            IF NOT EXISTS (
                SELECT 1
                  FROM orders AS source
                  JOIN order_source_price_snapshots AS snapshot
                    ON snapshot.source_price_snapshot_id = NEW.source_price_snapshot_id
                   AND snapshot.source_order_id = NEW.source_order_id
                  JOIN orders AS segment ON segment.order_id = NEW.segment_order_id
                 WHERE source.order_id = NEW.source_order_id
                   AND NOT source.is_deleted
                   AND NOT segment.is_deleted
                   AND source.channel = snapshot.channel
                   AND NEW.channel = source.channel
                   AND segment.stay_settlement_kind = 'company_sponsored'
                   AND NEW.segment_check_in_date = segment.check_in_date
                   AND NEW.segment_check_out_date = segment.check_out_date
                   AND NEW.segment_check_in_date >= snapshot.check_in_date
                   AND NEW.segment_check_out_date <= snapshot.check_out_date
                   AND NEW.calculation_base = (
                       SELECT coalesce(sum(entry.value::numeric), 0)
                         FROM jsonb_each_text(snapshot.nightly_bases) AS entry
                        WHERE entry.key::date >= NEW.segment_check_in_date
                          AND entry.key::date < NEW.segment_check_out_date
                   )
            ) THEN
                RAISE EXCEPTION 'company sponsorship root references must be consistent'
                    USING ERRCODE = '23514';
            END IF;

            IF (NEW.settlement_item_id IS NULL) <> (NEW.settlement_batch_id IS NULL)
               OR (NEW.status = 'settled' AND NEW.settlement_item_id IS NULL)
            THEN
                RAISE EXCEPTION 'settled sponsorship requires paired settlement references'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.settlement_item_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM owner_settlement_items AS item
                    WHERE item.item_id = NEW.settlement_item_id
                      AND item.settlement_id = NEW.settlement_batch_id
               )
            THEN
                RAISE EXCEPTION 'settlement item must belong to the same settlement batch'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_company_sponsored_stays_validate
        BEFORE INSERT OR UPDATE ON company_sponsored_stays
        FOR EACH ROW EXECUTE FUNCTION validate_company_sponsored_stay()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_company_sponsored_stays_no_delete
        BEFORE DELETE ON company_sponsored_stays
        FOR EACH ROW EXECUTE FUNCTION reject_sponsorship_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_sponsorship_order_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (OLD.channel IS DISTINCT FROM NEW.channel
                OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
                OR ((OLD.check_in_date IS DISTINCT FROM NEW.check_in_date
                     OR OLD.check_out_date IS DISTINCT FROM NEW.check_out_date)
                    AND NOT EXISTS (
                        SELECT 1 FROM managed_stay_groups AS managed
                         WHERE managed.source_order_id = OLD.order_id
                           AND OLD.stay_group_id IS NULL
                           AND NEW.stay_group_id = managed.stay_group_id
                           AND managed.kind = 'managed_split'
                           AND managed.version = 1
                    )))
               AND EXISTS (
                   SELECT 1 FROM order_source_price_snapshots AS snapshot
                    WHERE snapshot.source_order_id = OLD.order_id
               )
            THEN
                RAISE EXCEPTION 'order source-price identity is bound to immutable snapshots'
                    USING ERRCODE = '55000';
            END IF;
            IF (OLD.stay_settlement_kind IS DISTINCT FROM NEW.stay_settlement_kind
                OR OLD.check_in_date IS DISTINCT FROM NEW.check_in_date
                OR OLD.check_out_date IS DISTINCT FROM NEW.check_out_date
                OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
               AND EXISTS (
                   SELECT 1 FROM company_sponsored_stays AS sponsored
                    WHERE sponsored.segment_order_id = OLD.order_id
               )
            THEN
                RAISE EXCEPTION 'sponsored segment identity is bound to an immutable root'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_orders_protect_sponsorship_identity
        BEFORE UPDATE OF channel, check_in_date, check_out_date, stay_settlement_kind, is_deleted ON orders
        FOR EACH ROW EXECUTE FUNCTION protect_sponsorship_order_identity()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_company_sponsorship_adjustments_append_only
        BEFORE UPDATE OR DELETE ON company_sponsorship_adjustments
        FOR EACH ROW EXECUTE FUNCTION reject_sponsorship_immutable_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_sponsorship_settlement_correction_line()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.sponsorship_adjustment_id IS NOT NULL THEN
                    RAISE EXCEPTION 'sponsorship settlement correction lines are append-only'
                        USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.sponsorship_adjustment_id IS NOT NULL
               OR NEW.sponsorship_adjustment_id IS NOT NULL THEN
                RAISE EXCEPTION 'sponsorship settlement correction lines are append-only'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sponsorship_settlement_correction_line_immutable
        BEFORE UPDATE OR DELETE ON owner_settlement_items
        FOR EACH ROW EXECUTE FUNCTION protect_sponsorship_settlement_correction_line()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_sponsorship_settlement_correction_line_immutable "
        "ON owner_settlement_items"
    )
    op.execute("DROP FUNCTION protect_sponsorship_settlement_correction_line()")
    op.execute("DROP TRIGGER trg_orders_protect_sponsorship_identity ON orders")
    op.execute("DROP FUNCTION protect_sponsorship_order_identity()")
    op.execute("DROP TRIGGER trg_company_sponsored_stays_no_delete ON company_sponsored_stays")
    op.execute("DROP TRIGGER trg_company_sponsored_stays_validate ON company_sponsored_stays")
    op.execute("DROP FUNCTION validate_company_sponsored_stay()")
    op.execute("DROP TRIGGER trg_company_sponsored_stays_immutable_facts ON company_sponsored_stays")
    op.execute("DROP FUNCTION reject_sponsorship_root_fact_mutation()")
    op.execute("DROP TRIGGER trg_company_sponsorship_adjustments_append_only ON company_sponsorship_adjustments")
    op.execute("DROP TRIGGER trg_order_source_price_snapshots_immutable ON order_source_price_snapshots")
    op.execute("DROP FUNCTION reject_sponsorship_immutable_mutation()")
    op.execute("DROP TRIGGER trg_source_price_snapshot_insert ON order_source_price_snapshots")
    op.execute("DROP FUNCTION validate_source_price_snapshot_insert()")
    op.drop_index(
        "ix_sponsorship_adjustments_root_created",
        table_name="company_sponsorship_adjustments",
    )
    op.drop_constraint(
        "uq_owner_settlement_item_sponsorship_adjustment",
        "owner_settlement_items",
        type_="unique",
    )
    op.drop_constraint(
        "fk_owner_settlement_item_sponsorship_adjustment",
        "owner_settlement_items",
        type_="foreignkey",
    )
    op.drop_column("owner_settlement_items", "sponsorship_adjustment_id")
    op.drop_table("company_sponsorship_adjustments")
    op.drop_index(
        "uq_company_sponsored_stays_active_identity",
        table_name="company_sponsored_stays",
    )
    op.drop_index("ix_company_sponsored_stays_source", table_name="company_sponsored_stays")
    op.drop_table("company_sponsored_stays")
    op.drop_constraint(
        "uq_owner_settlement_items_item_batch",
        "owner_settlement_items",
        type_="unique",
    )
    op.drop_column("owner_settlement_items", "externally_settled_income")
    op.drop_index("ix_source_price_snapshots_source", table_name="order_source_price_snapshots")
    op.drop_table("order_source_price_snapshots")
    op.drop_column("orders", "stay_settlement_kind")

    sponsorship_status.drop(op.get_bind(), checkfirst=True)
    payment_responsibility.drop(op.get_bind(), checkfirst=True)
    snapshot_origin.drop(op.get_bind(), checkfirst=True)
    stay_settlement_kind.drop(op.get_bind(), checkfirst=True)
