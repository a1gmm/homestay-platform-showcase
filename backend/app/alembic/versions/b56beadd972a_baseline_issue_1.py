"""baseline (issue#1)

Revision ID: b56beadd972a
Revises:
Create Date: 2026-05-09 22:22:00.243902

锚点 migration。背景：

- 在 issue#1 之前，线上 Neon DB 是用 `Base.metadata.create_all` 首次部署起的，
  没有 alembic 迁移历史。
- 如果直接 `alembic revision --autogenerate` 会把整库当 diff 全量生成 CREATE
  TABLE，再让线上跑会因为表已存在而崩。
- 因此本 revision 留空 + 在生产跑一次 `alembic stamp head`，告诉 alembic
  "线上 DB 当前的版本就是这个 baseline"。
- 后续所有 schema 改动（issue#3 手机号 nullable / issue#6 booking_type +
  share_ratio 三字段 等）都基于这个 baseline 写正常的 upgrade/downgrade。

部署步骤（**必须由项目所有者在生产 Neon 上执行**）：
1. 合并本 PR 到 main
2. SSH 到 Railway 跑 `cd backend && alembic stamp head`
   或在 Neon SQL editor 手动跑：
   ```sql
   CREATE TABLE IF NOT EXISTS alembic_version (
     version_num VARCHAR(32) NOT NULL PRIMARY KEY
   );
   INSERT INTO alembic_version (version_num) VALUES ('b56beadd972a')
     ON CONFLICT (version_num) DO NOTHING;
   ```
3. 验证：`alembic current` 应返回 `b56beadd972a (head)`

Task 10 补充：原空 migration 让全新数据库无法执行 `alembic upgrade head`。
本 revision 现在只在数据库完全为空时创建 issue#1 当天冻结的基线 schema；
已有 `orders` 的历史/生产数据库继续 no-op，保持原 stamp 路径兼容。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b56beadd972a'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BASELINE_TABLES = {
    "audit_logs",
    "customers",
    "expenses",
    "guests",
    "notification_logs",
    "notifications",
    "orders",
    "owner_settlement_items",
    "owner_settlements",
    "owners",
    "payments",
    "pricing_records",
    "refunds",
    "room_blocks",
    "room_images",
    "rooms",
    "tasks",
    "users",
}


_BASELINE_DDL = (
    "CREATE TYPE user_role AS ENUM ('admin', 'operator', 'finance', 'cleaner', 'owner', 'keeper')",
    "CREATE TYPE room_status AS ENUM ('available', 'occupied', 'pending_clean', 'cleaning', 'maintenance', 'locked', 'reserved')",
    "CREATE TYPE channel AS ENUM ('ctrip', 'meituan', 'tujia', 'private', 'walk_in', 'direct')",
    "CREATE TYPE deposit_status AS ENUM ('not_collected', 'collected', 'returned', 'withheld')",
    "CREATE TYPE payment_status AS ENUM ('unpaid', 'partial', 'paid', 'partial_refund', 'full_refund')",
    "CREATE TYPE order_status AS ENUM ('pending_confirm', 'pending_payment', 'paid_pending_room', 'roomed_pending_checkin', 'checked_in', 'pending_checkout', 'completed', 'rescheduled', 'abnormal', 'cancelled')",
    "CREATE TYPE cleaning_status AS ENUM ('not_assigned', 'assigned', 'in_progress', 'done', 'inspected')",
    "CREATE TYPE payment_method AS ENUM ('wechat', 'alipay', 'cash', 'bank_transfer', 'platform', 'other')",
    "CREATE TYPE refund_reason AS ENUM ('guest_cancel', 'host_cancel', 'complaint', 'deposit_return', 'other')",
    "CREATE TYPE expense_category AS ENUM ('cleaning', 'maintenance', 'utilities', 'supplies', 'platform_fee', 'tax', 'other')",
    "CREATE TYPE expense_payer AS ENUM ('company', 'owner')",
    "CREATE TYPE task_type AS ENUM ('collect_deposit', 'cleaning', 'checkout_inspection', 'return_deposit', 'custom')",
    "CREATE TYPE task_status AS ENUM ('pending', 'in_progress', 'done', 'cancelled')",
    "CREATE TYPE task_priority AS ENUM ('low', 'medium', 'high', 'urgent')",
    "CREATE TYPE settlement_status AS ENUM ('pending', 'confirmed', 'paid', 'disputed')",
    """
    CREATE TABLE users (
        user_id VARCHAR(20) PRIMARY KEY,
        username VARCHAR(50) NOT NULL UNIQUE,
        display_name VARCHAR(50) NOT NULL,
        hashed_password VARCHAR(200) NOT NULL,
        role user_role NOT NULL,
        is_active BOOLEAN NOT NULL,
        phone VARCHAR(20),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE owners (
        owner_id VARCHAR(20) PRIMARY KEY,
        name VARCHAR(50) NOT NULL,
        phone VARCHAR(20),
        id_card VARCHAR(20),
        bank_account VARCHAR(50),
        bank_name VARCHAR(50),
        notes TEXT,
        password_hash VARCHAR(255),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE guests (
        guest_id VARCHAR(20) PRIMARY KEY,
        name VARCHAR(50) NOT NULL,
        phone VARCHAR(20) NOT NULL UNIQUE,
        id_number VARCHAR(30),
        wechat VARCHAR(50),
        notes TEXT,
        visit_count INTEGER NOT NULL,
        total_spent NUMERIC(12, 2) NOT NULL,
        total_nights INTEGER NOT NULL,
        last_check_in TIMESTAMPTZ,
        preferred_room VARCHAR(10),
        tags VARCHAR(200),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX ix_guests_phone ON guests (phone)",
    "CREATE INDEX ix_guests_name ON guests (name)",
    """
    CREATE TABLE notification_logs (
        log_id VARCHAR(20) PRIMARY KEY,
        order_id VARCHAR(20),
        template_name VARCHAR(50),
        channel VARCHAR(20) NOT NULL,
        recipient VARCHAR(100),
        content TEXT,
        status VARCHAR(20) NOT NULL,
        error_message TEXT,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE customers (
        phone VARCHAR(20) PRIMARY KEY,
        name VARCHAR(50),
        id_number VARCHAR(30),
        wechat_openid VARCHAR(64) UNIQUE,
        last_login_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE rooms (
        room_id VARCHAR(10) PRIMARY KEY,
        room_name VARCHAR(50) NOT NULL,
        room_type VARCHAR(64),
        floor SMALLINT,
        owner_id VARCHAR(20) REFERENCES owners (owner_id),
        owner_share_ratio NUMERIC(4, 3) NOT NULL,
        owner_deduction_rules JSONB DEFAULT '[]' NOT NULL,
        owner_ignored_categories JSONB DEFAULT '[]' NOT NULL,
        room_status room_status NOT NULL,
        base_price NUMERIC(10, 2),
        province VARCHAR(20),
        city VARCHAR(20),
        district VARCHAR(30),
        community_name VARCHAR(50),
        building_no VARCHAR(20),
        unit_no VARCHAR(10),
        min_stay_nights INTEGER NOT NULL,
        weekend_markup NUMERIC(5, 2) NOT NULL,
        holiday_markup NUMERIC(5, 2) NOT NULL,
        channel_availability JSONB NOT NULL,
        metadata JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE owner_settlements (
        settlement_id VARCHAR(20) PRIMARY KEY,
        owner_id VARCHAR(20) NOT NULL REFERENCES owners (owner_id),
        billing_month VARCHAR(7) NOT NULL,
        total_net_revenue NUMERIC(10, 2) NOT NULL,
        owner_amount NUMERIC(10, 2) NOT NULL,
        deducted_expenses NUMERIC(10, 2) NOT NULL,
        actual_owner_amount NUMERIC(10, 2) NOT NULL,
        status settlement_status NOT NULL,
        payment_date DATE,
        doc_url VARCHAR(500),
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE audit_logs (
        log_id BIGSERIAL PRIMARY KEY,
        operator_id VARCHAR(20) REFERENCES users (user_id),
        action VARCHAR(100) NOT NULL,
        resource_type VARCHAR(50),
        resource_id VARCHAR(50),
        before_data JSONB,
        after_data JSONB,
        ip_address VARCHAR(50),
        user_agent VARCHAR(300),
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE INDEX ix_audit_created ON audit_logs (created_at)",
    "CREATE INDEX ix_audit_resource ON audit_logs (resource_type, resource_id)",
    """
    CREATE TABLE notifications (
        notification_id VARCHAR(20) PRIMARY KEY,
        user_id VARCHAR(20) REFERENCES users (user_id),
        title VARCHAR(200) NOT NULL,
        content TEXT,
        type VARCHAR(30) NOT NULL,
        is_read BOOLEAN NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE orders (
        order_id VARCHAR(20) PRIMARY KEY,
        channel channel NOT NULL,
        platform_order_id VARCHAR(100),
        guest_name VARCHAR(50) NOT NULL,
        guest_phone VARCHAR(20) NOT NULL,
        room_id VARCHAR(10) REFERENCES rooms (room_id),
        check_in_date DATE NOT NULL,
        check_out_date DATE NOT NULL,
        list_price NUMERIC(10, 2),
        discount_amount NUMERIC(10, 2) NOT NULL,
        actual_price NUMERIC(10, 2),
        deposit NUMERIC(10, 2) NOT NULL,
        deposit_status deposit_status NOT NULL,
        payment_status payment_status NOT NULL,
        order_status order_status NOT NULL,
        cleaning_status cleaning_status NOT NULL,
        platform_commission_rate NUMERIC(5, 4) NOT NULL,
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        is_deleted BOOLEAN NOT NULL,
        metadata JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE INDEX ix_orders_checkin ON orders (check_in_date)",
    "CREATE INDEX ix_orders_status ON orders (order_status, is_deleted)",
    "CREATE INDEX ix_orders_created ON orders (created_at)",
    "CREATE INDEX ix_orders_room_dates ON orders (room_id, check_in_date, check_out_date)",
    """
    CREATE TABLE pricing_records (
        pricing_id VARCHAR(20) PRIMARY KEY,
        room_id VARCHAR(10) REFERENCES rooms (room_id),
        effective_date DATE NOT NULL,
        price NUMERIC(10, 2) NOT NULL,
        recommended_price NUMERIC(10, 2),
        base_price NUMERIC(10, 2),
        competitor_avg_price NUMERIC(10, 2),
        algorithm_factors JSONB,
        source VARCHAR(50),
        source_url TEXT,
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        CONSTRAINT uq_pricing_room_date UNIQUE (room_id, effective_date)
    )
    """,
    "CREATE INDEX ix_pricing_room_date ON pricing_records (room_id, effective_date)",
    """
    CREATE TABLE owner_settlement_items (
        item_id VARCHAR(20) PRIMARY KEY,
        settlement_id VARCHAR(20) NOT NULL REFERENCES owner_settlements (settlement_id) ON DELETE CASCADE,
        room_id VARCHAR(10) NOT NULL REFERENCES rooms (room_id),
        order_count INTEGER NOT NULL,
        revenue NUMERIC(10, 2) NOT NULL,
        commission NUMERIC(10, 2) NOT NULL,
        net_revenue NUMERIC(10, 2) NOT NULL,
        owner_expenses NUMERIC(10, 2) NOT NULL,
        share_ratio_snapshot NUMERIC(4, 3) NOT NULL,
        owner_net_amount NUMERIC(10, 2) NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE room_blocks (
        block_id VARCHAR(20) PRIMARY KEY,
        room_id VARCHAR(10) NOT NULL REFERENCES rooms (room_id),
        block_type VARCHAR(20) NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        reason TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE INDEX ix_room_blocks_room_dates ON room_blocks (room_id, start_date, end_date)",
    """
    CREATE TABLE room_images (
        image_id VARCHAR(20) PRIMARY KEY,
        room_id VARCHAR(10) NOT NULL REFERENCES rooms (room_id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        object_key TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        is_cover BOOLEAN NOT NULL,
        content_type VARCHAR(50),
        size_bytes INTEGER,
        uploaded_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE INDEX ix_room_images_room_sort ON room_images (room_id, sort_order)",
    """
    CREATE TABLE payments (
        payment_id VARCHAR(20) PRIMARY KEY,
        order_id VARCHAR(20) NOT NULL REFERENCES orders (order_id),
        amount NUMERIC(10, 2) NOT NULL,
        method payment_method NOT NULL,
        paid_at TIMESTAMPTZ NOT NULL,
        is_deposit BOOLEAN NOT NULL,
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE refunds (
        refund_id VARCHAR(20) PRIMARY KEY,
        order_id VARCHAR(20) NOT NULL REFERENCES orders (order_id),
        amount NUMERIC(10, 2) NOT NULL,
        reason refund_reason NOT NULL,
        refunded_at TIMESTAMPTZ NOT NULL,
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    """
    CREATE TABLE expenses (
        expense_id VARCHAR(20) PRIMARY KEY,
        category expense_category NOT NULL,
        amount NUMERIC(10, 2) NOT NULL,
        description VARCHAR(200) NOT NULL,
        expense_date DATE NOT NULL,
        room_id VARCHAR(10) REFERENCES rooms (room_id),
        order_id VARCHAR(20) REFERENCES orders (order_id),
        payer expense_payer NOT NULL,
        owner_id VARCHAR(20) REFERENCES owners (owner_id),
        receipt_url VARCHAR(500),
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE INDEX ix_expenses_room_date ON expenses (room_id, expense_date)",
    "CREATE INDEX ix_expenses_date ON expenses (expense_date)",
    """
    CREATE TABLE tasks (
        task_id VARCHAR(20) PRIMARY KEY,
        task_type task_type NOT NULL,
        title VARCHAR(100) NOT NULL,
        description TEXT,
        order_id VARCHAR(20) REFERENCES orders (order_id),
        room_id VARCHAR(10) REFERENCES rooms (room_id),
        assignee_id VARCHAR(20) REFERENCES users (user_id),
        status task_status NOT NULL,
        priority task_priority NOT NULL,
        deadline TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        notes TEXT,
        created_by VARCHAR(20) REFERENCES users (user_id),
        submitted_at TIMESTAMPTZ,
        review_status VARCHAR(20),
        reviewer_id VARCHAR(20),
        reviewed_at TIMESTAMPTZ,
        rejection_reason TEXT,
        created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
    )
    """,
    "CREATE INDEX ix_tasks_assignee_status ON tasks (assignee_id, status)",
    "CREATE INDEX ix_tasks_deadline_status ON tasks (deadline, status)",
    "CREATE INDEX ix_tasks_order ON tasks (order_id)",
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {table for table in _BASELINE_TABLES if inspector.has_table(table)}
    if "orders" in existing:
        return
    if existing:
        raise RuntimeError(
            "Refusing to infer a partial pre-Alembic baseline; existing tables: "
            + ", ".join(sorted(existing))
        )
    for statement in _BASELINE_DDL:
        op.execute(sa.text(statement))


def downgrade() -> None:
    # Historical stamped databases predate Alembic ownership of these tables;
    # never drop their schema when downgrading across the compatibility anchor.
    pass
