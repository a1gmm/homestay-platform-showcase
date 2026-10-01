"""Serialize platform identity claims across admin reconciliation entry points."""

from sqlalchemy import select, text

from app.models.order import Order


async def check_platform_identity_available(db, platform_id, order_id, *, lock=True):
    if lock and db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtext('order-platform-identity'), hashtext(:identity))"
            ),
            {"identity": platform_id},
        )
    duplicate = await db.scalar(
        select(Order.order_id)
        .where(
            Order.platform_order_id == platform_id,
            Order.order_id != order_id,
            Order.is_deleted.is_(False),
        )
        .limit(1)
    )
    if duplicate:
        raise ValueError("该平台单号已关联其他系统订单，请人工核对")
