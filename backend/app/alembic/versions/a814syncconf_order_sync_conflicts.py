"""Add manual-sync conflicts, durable operation claims, and managed stay groups.

Revision ID: a814syncconf
Revises: billrecon0802
Create Date: 2026-08-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a814syncconf"
down_revision = "billrecon0802"
branch_labels = None
depends_on = None


conflict_status = sa.Enum("open", "ignored", "resolved", name="order_sync_conflict_status")
operation_status = sa.Enum("in_progress", "succeeded", "failed", name="order_operation_status")
managed_group_kind = sa.Enum("managed_split", name="managed_stay_group_kind")


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.create_table(
        "order_sync_conflicts",
        sa.Column("conflict_id", sa.String(length=40), primary_key=True),
        sa.Column("source_order_id", sa.String(length=20), sa.ForeignKey("orders.order_id"), nullable=False),
        sa.Column("field", sa.String(length=50), nullable=False),
        sa.Column("local_value", postgresql.JSONB, nullable=False),
        sa.Column("upstream_value", postgresql.JSONB, nullable=False),
        sa.Column("upstream_version", sa.String(length=128), nullable=False),
        sa.Column("status", conflict_status, nullable=False, server_default="open"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("ignored_by", sa.String(length=20), sa.ForeignKey("users.user_id")),
        sa.Column("ignored_at", sa.DateTime(timezone=True)),
        sa.Column("ignored_audit_log_id", sa.BigInteger(), sa.ForeignKey("audit_logs.log_id")),
        sa.Column("resolved_by", sa.String(length=20), sa.ForeignKey("users.user_id")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_audit_log_id", sa.BigInteger(), sa.ForeignKey("audit_logs.log_id")),
        sa.UniqueConstraint(
            "source_order_id", "field", "upstream_version",
            name="uq_order_sync_conflicts_source_field_version",
        ),
    )
    op.create_index(
        "ix_order_sync_conflicts_source_status",
        "order_sync_conflicts", ["source_order_id", "status"],
    )
    op.create_index(
        "ix_order_sync_conflicts_status_last_seen",
        "order_sync_conflicts", ["status", "last_seen_at"],
    )

    op.create_table(
        "order_operations",
        sa.Column("operation_id", sa.String(length=40), primary_key=True),
        sa.Column("property_scope", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", operation_status, nullable=False, server_default="in_progress"),
        sa.Column("result_order_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("result_stay_group_id", sa.String(length=32)),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "property_scope", "operation", "idempotency_key",
            name="uq_order_operations_scope_operation_key",
        ),
    )
    op.create_index("ix_order_operations_status", "order_operations", ["status"])

    op.create_table(
        "managed_stay_groups",
        sa.Column("stay_group_id", sa.String(length=32), primary_key=True),
        sa.Column(
            "source_order_id", sa.String(length=20), sa.ForeignKey("orders.order_id"),
            nullable=False, unique=True,
        ),
        sa.Column("kind", managed_group_kind, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("version >= 1", name="ck_managed_stay_group_positive_version"),
    )


def downgrade() -> None:
    op.drop_table("managed_stay_groups")
    op.drop_index("ix_order_operations_status", table_name="order_operations")
    op.drop_table("order_operations")
    op.drop_index("ix_order_sync_conflicts_status_last_seen", table_name="order_sync_conflicts")
    op.drop_index("ix_order_sync_conflicts_source_status", table_name="order_sync_conflicts")
    op.drop_table("order_sync_conflicts")
    managed_group_kind.drop(op.get_bind(), checkfirst=True)
    operation_status.drop(op.get_bind(), checkfirst=True)
    conflict_status.drop(op.get_bind(), checkfirst=True)
