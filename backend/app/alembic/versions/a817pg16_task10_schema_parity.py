"""Close model/schema drift required by the PostgreSQL 16 verification gate.

Revision ID: a817pg16
Revises: a817srcovr
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a817pg16"
down_revision = "a817srcovr"
branch_labels = None
depends_on = None


def _add_column_if_missing(
    table_name: str,
    column: sa.Column,
    existing_columns: dict[str, set[str]],
) -> None:
    if column.name not in existing_columns[table_name]:
        op.add_column(table_name, column)
        existing_columns[table_name].add(column.name)


def _has_foreign_key(
    inspector: sa.Inspector,
    table_name: str,
    constrained_column: str,
) -> bool:
    return any(
        foreign_key.get("constrained_columns") == [constrained_column]
        and foreign_key.get("referred_table") == "users"
        and foreign_key.get("referred_columns") == ["user_id"]
        for foreign_key in inspector.get_foreign_keys(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {
        table_name: {
            column["name"] for column in inspector.get_columns(table_name)
        }
        for table_name in ("expenses", "owners", "refunds", "rooms")
    }

    # Several of these columns were historically created by app.main's
    # idempotent startup DDL. Adopt those columns when present so a stamped,
    # production-shaped database can run the same migration as a fresh one.
    _add_column_if_missing(
        "expenses",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        existing_columns,
    )
    _add_column_if_missing(
        "expenses", sa.Column("deleted_at", sa.DateTime(timezone=True)), existing_columns
    )
    _add_column_if_missing(
        "expenses", sa.Column("deleted_by", sa.String(length=20)), existing_columns
    )
    op.execute(sa.text("UPDATE expenses SET is_deleted = false WHERE is_deleted IS NULL"))
    op.alter_column(
        "expenses",
        "is_deleted",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    )
    if not _has_foreign_key(inspector, "expenses", "deleted_by"):
        op.create_foreign_key(
            "fk_expenses_deleted_by_users",
            "expenses",
            "users",
            ["deleted_by"],
            ["user_id"],
        )

    _add_column_if_missing(
        "owners", sa.Column("username", sa.String(length=50)), existing_columns
    )
    has_username_unique_constraint = any(
        constraint.get("column_names") == ["username"]
        for constraint in inspector.get_unique_constraints("owners")
    )
    if not has_username_unique_constraint:
        op.create_unique_constraint("uq_owners_username", "owners", ["username"])
    # app.main may already own the equivalent partial unique index.  Keep it: this
    # compatibility revision must never destroy an out-of-band production object.
    # Alembic autogeneration explicitly ignores that legacy compatibility index.

    _add_column_if_missing(
        "refunds",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        existing_columns,
    )
    _add_column_if_missing(
        "refunds", sa.Column("deleted_at", sa.DateTime(timezone=True)), existing_columns
    )
    _add_column_if_missing(
        "refunds", sa.Column("deleted_by", sa.String(length=20)), existing_columns
    )
    op.execute(sa.text("UPDATE refunds SET is_deleted = false WHERE is_deleted IS NULL"))
    op.alter_column(
        "refunds",
        "is_deleted",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    )
    if not _has_foreign_key(inspector, "refunds", "deleted_by"):
        op.create_foreign_key(
            "fk_refunds_deleted_by_users",
            "refunds",
            "users",
            ["deleted_by"],
            ["user_id"],
        )

    _add_column_if_missing(
        "rooms", sa.Column("contract_signed_date", sa.Date()), existing_columns
    )
    _add_column_if_missing("rooms", sa.Column("sale_date", sa.Date()), existing_columns)
    _add_column_if_missing("rooms", sa.Column("remarks", sa.Text()), existing_columns)
    _add_column_if_missing(
        "rooms",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        existing_columns,
    )
    _add_column_if_missing(
        "rooms", sa.Column("deleted_at", sa.DateTime(timezone=True)), existing_columns
    )
    op.execute(sa.text("UPDATE rooms SET is_deleted = false WHERE is_deleted IS NULL"))
    op.alter_column(
        "rooms",
        "is_deleted",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    )

    for table_name in ("recon_batches", "recon_diffs"):
        op.execute(sa.text(f"UPDATE {table_name} SET created_at = now() WHERE created_at IS NULL"))
        op.alter_column(
            table_name,
            "created_at",
            existing_type=sa.DateTime(timezone=True),
            existing_server_default=sa.func.now(),
            nullable=False,
        )

    op.execute(
        sa.text(
            "UPDATE service_fee_config SET updated_at = now() WHERE updated_at IS NULL"
        )
    )
    op.alter_column(
        "service_fee_config",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )


def downgrade() -> None:
    # Fail closed.  This revision adopts columns/indexes historically created by
    # application startup and manual operational SQL, so it cannot prove object
    # ownership during a later Alembic process.  Downgrading only moves the
    # revision marker; the additive compatibility schema and its data remain.
    # Re-upgrade is idempotent through the inspector checks in ``upgrade``.
    pass
