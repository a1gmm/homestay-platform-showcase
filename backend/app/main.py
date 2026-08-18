from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import sentry_sdk
import logging
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.core.keepalive import start_keepalive, stop_keepalive

# Ensure app loggers write to stdout at INFO level. Without this, uvicorn's default
# logging config swallows our request-timing middleware output, so Railway logs only
# show uvicorn's access lines (no per-request ms). Idempotent — uvicorn calls
# logging.basicConfig itself, but only adds handlers if root has none.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
logging.getLogger("app").setLevel(logging.INFO)

logger = logging.getLogger(__name__)
from app.api.v1 import auth, orders, rooms, finance, tasks, dashboard, audit, guests, settlements, room_blocks, notifications, export as export_api, search as search_api, owners, booking, customer_auth, owner_portal, staff_auth, staff_portal, unified_auth, admin_demo, hosting_leads, feishu_callback, reconciliation, lock_events, deposit_receipt, billing_recon, assistant

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        environment=settings.APP_ENV,
    )


async def _bound_locks(conn) -> None:
    """给启动 DDL 事务设 lock_timeout，避免等锁无限挂起拖垮 startup。

    没有它时，被别的会话持锁挡住的 ALTER/CREATE 会永久阻塞（Postgres 默认
    lock_timeout=0=不超时），应用卡在「Waiting for application startup」→ 502
    （2026-07-19 生产事故：idle-in-transaction 僵尸连接持锁，重启无效需人工杀连接）。
    设 5s 上限后，等锁超时会抛 LockNotAvailableError，由每个 DDL 块外层已有的
    try/except log-and-skip——这些 DDL 都是幂等 schema 补丁，跳过一轮安全，真正
    迁移走 alembic。SET LOCAL 只作用于当前事务，不影响后续正常查询。"""
    await conn.execute(text("SET LOCAL lock_timeout = '5s'"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: lightweight schema adjustments (idempotent)
    try:
        async with engine.begin() as conn:
            await _bound_locks(conn)
            # Rooms location columns
            await conn.execute(
                text(
                    """
                    ALTER TABLE rooms
                    ADD COLUMN IF NOT EXISTS province VARCHAR(20),
                    ADD COLUMN IF NOT EXISTS city VARCHAR(20),
                    ADD COLUMN IF NOT EXISTS district VARCHAR(30),
                    ADD COLUMN IF NOT EXISTS community_name VARCHAR(50),
                    ADD COLUMN IF NOT EXISTS building_no VARCHAR(20),
                    ADD COLUMN IF NOT EXISTS unit_no VARCHAR(10),
                    ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ
                    """
                )
            )
            # Guests table
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS guests (
                        guest_id VARCHAR(20) PRIMARY KEY,
                        name VARCHAR(50) NOT NULL,
                        phone VARCHAR(20) NOT NULL UNIQUE,
                        id_number VARCHAR(30),
                        wechat VARCHAR(50),
                        notes TEXT,
                        visit_count INTEGER DEFAULT 0,
                        total_spent NUMERIC(12,2) DEFAULT 0,
                        total_nights INTEGER DEFAULT 0,
                        last_check_in TIMESTAMPTZ,
                        preferred_room VARCHAR(10),
                        tags VARCHAR(200),
                        created_at TIMESTAMPTZ DEFAULT now(),
                        updated_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            # Room blocks table
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS room_blocks (
                        block_id VARCHAR(20) PRIMARY KEY,
                        room_id VARCHAR(10) NOT NULL REFERENCES rooms(room_id),
                        block_type VARCHAR(20) NOT NULL,
                        start_date DATE NOT NULL,
                        end_date DATE NOT NULL,
                        reason TEXT,
                        created_by VARCHAR(20),
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            # Pricing records dynamic columns
            await conn.execute(
                text(
                    """
                    ALTER TABLE pricing_records
                    ADD COLUMN IF NOT EXISTS recommended_price NUMERIC(10, 2),
                    ADD COLUMN IF NOT EXISTS base_price NUMERIC(10, 2),
                    ADD COLUMN IF NOT EXISTS competitor_avg_price NUMERIC(10, 2),
                    ADD COLUMN IF NOT EXISTS algorithm_factors JSONB
                    """
                )
            )
            # Pricing unique constraint (idempotent)
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'uq_pricing_room_date'
                        ) THEN
                            ALTER TABLE pricing_records
                            ADD CONSTRAINT uq_pricing_room_date UNIQUE (room_id, effective_date);
                        END IF;
                    END $$
                    """
                )
            )
            # Feature 4: Task review workflow columns
            await conn.execute(
                text(
                    """
                    ALTER TABLE tasks
                    ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS review_status VARCHAR(20),
                    ADD COLUMN IF NOT EXISTS reviewer_id VARCHAR(20),
                    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS rejection_reason TEXT
                    """
                )
            )
            # Feature 8: Room min stay & price rules
            await conn.execute(
                text(
                    """
                    ALTER TABLE rooms
                    ADD COLUMN IF NOT EXISTS min_stay_nights INTEGER DEFAULT 1,
                    ADD COLUMN IF NOT EXISTS weekend_markup NUMERIC(5, 2) DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS holiday_markup NUMERIC(5, 2) DEFAULT 0
                    """
                )
            )
            # Feature 10: Notifications table
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS notifications (
                        notification_id VARCHAR(20) PRIMARY KEY,
                        user_id VARCHAR(20) REFERENCES users(user_id),
                        title VARCHAR(200) NOT NULL,
                        content TEXT,
                        type VARCHAR(30) DEFAULT 'system',
                        is_read BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            # Feature 3: Notification logs table
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS notification_logs (
                        log_id VARCHAR(20) PRIMARY KEY,
                        order_id VARCHAR(20),
                        template_name VARCHAR(50),
                        channel VARCHAR(20) DEFAULT 'feishu',
                        recipient VARCHAR(100),
                        content TEXT,
                        status VARCHAR(20) DEFAULT 'sent',
                        error_message TEXT,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            # Owner profit-sharing: per-room deduction rules
            await conn.execute(
                text(
                    """
                    ALTER TABLE rooms
                    ADD COLUMN IF NOT EXISTS owner_deduction_rules JSONB DEFAULT '[]'::jsonb,
                    ADD COLUMN IF NOT EXISTS owner_ignored_categories JSONB DEFAULT '[]'::jsonb
                    """
                )
            )
            # Owner settlement per-room snapshot items
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS owner_settlement_items (
                        item_id VARCHAR(20) PRIMARY KEY,
                        settlement_id VARCHAR(20) NOT NULL REFERENCES owner_settlements(settlement_id) ON DELETE CASCADE,
                        room_id VARCHAR(10) NOT NULL REFERENCES rooms(room_id),
                        order_count INTEGER DEFAULT 0,
                        revenue NUMERIC(10, 2) DEFAULT 0,
                        commission NUMERIC(10, 2) DEFAULT 0,
                        net_revenue NUMERIC(10, 2) DEFAULT 0,
                        owner_expenses NUMERIC(10, 2) DEFAULT 0,
                        share_ratio_snapshot NUMERIC(4, 3) DEFAULT 0.600,
                        owner_net_amount NUMERIC(10, 2) DEFAULT 0,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_settlement_items_settlement ON owner_settlement_items(settlement_id)"
                )
            )
            # Room images (per-room photo gallery, OSS-backed)
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS room_images (
                        image_id VARCHAR(20) PRIMARY KEY,
                        room_id VARCHAR(10) NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
                        url TEXT NOT NULL,
                        object_key TEXT NOT NULL,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        is_cover BOOLEAN NOT NULL DEFAULT FALSE,
                        content_type VARCHAR(50),
                        size_bytes INTEGER,
                        uploaded_by VARCHAR(20) REFERENCES users(user_id),
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_room_images_room_sort ON room_images(room_id, sort_order)"
                )
            )
            # C-side: customers table
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS customers (
                        phone VARCHAR(20) PRIMARY KEY,
                        name VARCHAR(50),
                        id_number VARCHAR(30),
                        wechat_openid VARCHAR(64) UNIQUE,
                        last_login_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ DEFAULT now(),
                        updated_at TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
            )
            # Owner portal: unique index on owners.phone (for OTP login lookup)
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_owners_phone ON owners(phone) WHERE phone IS NOT NULL"
                )
            )
            # Staff portal: unique index on users.phone (for OTP login lookup)
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_phone ON users(phone) WHERE phone IS NOT NULL"
                )
            )
            # Staff portal: add 'keeper' value to user_role enum (idempotent)
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_enum e
                            JOIN pg_type t ON t.oid = e.enumtypid
                            WHERE t.typname = 'user_role' AND e.enumlabel = 'keeper'
                        ) THEN
                            ALTER TYPE user_role ADD VALUE 'keeper';
                        END IF;
                    END $$
                    """
                )
            )
            # C-side: add 'direct' value to channel enum (idempotent)
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_enum e
                            JOIN pg_type t ON t.oid = e.enumtypid
                            WHERE t.typname = 'channel' AND e.enumlabel = 'direct'
                        ) THEN
                            ALTER TYPE channel ADD VALUE 'direct';
                        END IF;
                    END $$
                    """
                )
            )
            # Owners: password_hash column for username+password login (in addition to OTP)
            await conn.execute(
                text(
                    """
                    ALTER TABLE owners
                    ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)
                    """
                )
            )
            # Owners: username column for account+password login (替代 phone)
            await conn.execute(
                text(
                    """
                    ALTER TABLE owners
                    ADD COLUMN IF NOT EXISTS username VARCHAR(50)
                    """
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_owners_username ON owners(username) WHERE username IS NOT NULL"
                )
            )
            # 批2 item6/item7: 退款/支出软删除列（口径同 Payment 软删）
            await conn.execute(
                text(
                    """
                    ALTER TABLE refunds
                    ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(20)
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    ALTER TABLE expenses
                    ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(20)
                    """
                )
            )
            # RoomStatus: add 'pending_clean' (退房后等保洁认领) + 'cleaning' (保洁清扫中)
            for new_value in ("pending_clean", "cleaning"):
                await conn.execute(
                    text(
                        f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_enum e
                                JOIN pg_type t ON t.oid = e.enumtypid
                                WHERE t.typname = 'room_status' AND e.enumlabel = '{new_value}'
                            ) THEN
                                ALTER TYPE room_status ADD VALUE '{new_value}';
                            END IF;
                        END $$
                        """
                    )
                )
    except Exception as e:
        logger.warning("Schema auto-migration skipped or failed: %s", e)

    # Performance indexes (idempotent, safe to run every startup)
    try:
        async with engine.begin() as conn:
            await _bound_locks(conn)
            for stmt in (
                "CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(expense_date)",
                "CREATE INDEX IF NOT EXISTS ix_expenses_room_date ON expenses(room_id, expense_date)",
                "CREATE INDEX IF NOT EXISTS ix_tasks_deadline_status ON tasks(deadline, status)",
                "CREATE INDEX IF NOT EXISTS ix_tasks_order ON tasks(order_id)",
                "CREATE INDEX IF NOT EXISTS ix_tasks_assignee_status ON tasks(assignee_id, status)",
                "CREATE INDEX IF NOT EXISTS ix_orders_checkout ON orders(check_out_date)",
                "CREATE INDEX IF NOT EXISTS ix_payments_order ON payments(order_id)",
                "CREATE INDEX IF NOT EXISTS ix_orders_guest_phone ON orders(guest_phone)",
                "CREATE INDEX IF NOT EXISTS ix_payments_order_paid_at ON payments(order_id, paid_at)",
                "CREATE INDEX IF NOT EXISTS ix_refunds_order ON refunds(order_id)",
                "CREATE INDEX IF NOT EXISTS ix_expenses_payer ON expenses(payer)",
            ):
                await conn.execute(text(stmt))
    except Exception as e:
        logger.warning("Index creation skipped: %s", e)

    # DB-level exclusion constraint: one room can't have two overlapping active orders.
    # Defense-in-depth on top of the application-level check_room_conflict — immune to
    # race conditions regardless of SELECT FOR UPDATE ordering. Requires btree_gist.
    # Failure is non-fatal: if the extension isn't granted or existing data already
    # violates the constraint, we warn and keep the app up so operators can reconcile.
    try:
        async with engine.begin() as conn:
            await _bound_locks(conn)
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            await conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint WHERE conname = 'orders_no_room_overlap'
                        ) THEN
                            ALTER TABLE orders
                            ADD CONSTRAINT orders_no_room_overlap
                            EXCLUDE USING gist (
                                room_id WITH =,
                                daterange(check_in_date, check_out_date, '[)') WITH &&
                            ) WHERE (
                                room_id IS NOT NULL
                                AND is_deleted = false
                                AND order_status::text NOT IN ('cancelled', 'completed')
                            );
                        END IF;
                    END $$
                    """
                )
            )
    except Exception as e:
        logger.warning(
            "Room exclusion constraint skipped (likely overlapping legacy data — needs cleanup): %s",
            e,
        )

    # Start DB keepalive — prevents Neon serverless cold-start (5-18s) on idle workloads.
    keepalive_task = start_keepalive(engine, settings.DB_KEEPALIVE_INTERVAL_SECONDS)

    # 飞书长连接（卡片回传交互）：best-effort，任何失败只 log 不阻断启动。
    # 先记下主 event loop——ws 处理器要把 DB 协程 submit 回这个 loop（engine 绑定于此）。
    try:
        import asyncio as _asyncio

        from app.services.feishu_ws import set_main_loop, start_ws_client_in_thread
        set_main_loop(_asyncio.get_running_loop())
        start_ws_client_in_thread()
    except Exception:  # noqa: BLE001
        logger.warning("飞书长连接启动失败（卡片按钮将不可用）", exc_info=True)

    yield

    # Shutdown: cancel keepalive, then connection cleanup
    await stop_keepalive(keepalive_task)


_is_prod = settings.APP_ENV == "production"
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    docs_url=None if _is_prod else "/api/docs",
    redoc_url=None if _is_prod else "/api/redoc",
    openapi_url=None if _is_prod else "/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=settings.CORS_METHODS,
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s → %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if _is_prod:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

# Register routers
app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(orders.router, prefix=settings.API_PREFIX)
app.include_router(rooms.router, prefix=settings.API_PREFIX)
app.include_router(finance.router, prefix=settings.API_PREFIX)
app.include_router(tasks.router, prefix=settings.API_PREFIX)
app.include_router(dashboard.router, prefix=settings.API_PREFIX)
app.include_router(audit.router, prefix=settings.API_PREFIX)
app.include_router(guests.router, prefix=settings.API_PREFIX)
app.include_router(settlements.router, prefix=settings.API_PREFIX)
app.include_router(room_blocks.router, prefix=settings.API_PREFIX)
app.include_router(notifications.router, prefix=settings.API_PREFIX)
app.include_router(export_api.router, prefix=settings.API_PREFIX)
app.include_router(search_api.router, prefix=settings.API_PREFIX)
app.include_router(owners.router, prefix=settings.API_PREFIX)
app.include_router(booking.router, prefix=settings.API_PREFIX)
app.include_router(customer_auth.router, prefix=settings.API_PREFIX)
app.include_router(owner_portal.router, prefix=settings.API_PREFIX)
app.include_router(staff_auth.router, prefix=settings.API_PREFIX)
app.include_router(staff_portal.router, prefix=settings.API_PREFIX)
app.include_router(unified_auth.router, prefix=settings.API_PREFIX)
app.include_router(hosting_leads.router, prefix=settings.API_PREFIX)
app.include_router(feishu_callback.router, prefix=settings.API_PREFIX)
app.include_router(reconciliation.router, prefix=settings.API_PREFIX)
app.include_router(lock_events.router, prefix=settings.API_PREFIX)
app.include_router(deposit_receipt.router, prefix=settings.API_PREFIX)
app.include_router(billing_recon.router, prefix=settings.API_PREFIX)
app.include_router(assistant.router, prefix=settings.API_PREFIX)

# admin_demo 路由含演示账号种子（硬编码密码），生产环境不注册，避免凭证泄露面。
if settings.APP_ENV != "production":
    app.include_router(admin_demo.router, prefix=settings.API_PREFIX)


@app.get("/health")
async def health():
    checks = {"api": "ok"}

    # Check database connectivity
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {type(e).__name__}"

    # Check Redis connectivity (bounded — default redis socket timeout is unbounded
    # which made unhealthy /health responses block ~1s waiting for a TCP timeout).
    try:
        from app.core.deps import get_redis
        redis = await get_redis()
        await asyncio.wait_for(redis.ping(), timeout=1.0)
        checks["redis"] = "ok"
    except asyncio.TimeoutError:
        checks["redis"] = "error: TimeoutError"
    except Exception as e:
        checks["redis"] = f"error: {type(e).__name__}"

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "version": "1.0.0",
        "checks": checks,
    }
