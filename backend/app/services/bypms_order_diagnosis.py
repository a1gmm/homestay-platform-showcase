"""Read-only, bounded order trace. Never return source payload or guest identity."""
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.order_sync_conflict import OrderSyncConflict, OrderSyncConflictStatus
from app.schemas.bypms_sync import BypmsOrderDiagnosis
from app.services.bypms_sync_admin import IntegrationTablesUnavailable, _as_utc, _execute_integration, _stale_after_seconds
from app.services.manual_override import locked_fields

_source = Table(
    "ota_raw_orders", MetaData(),
    Column("platform", String), Column("platform_order_id", String),
    Column("fetched_at", DateTime(timezone=True)), Column("status", String),
    Column("guest_name", String), Column("check_in", Date), Column("check_out", Date),
)


async def diagnose_order(db: AsyncSession, platform_order_id: str, now: datetime) -> BypmsOrderDiagnosis:
    try:
        rows = (await _execute_integration(db, select(_source).where(
            _source.c.platform == "bypms", _source.c.platform_order_id == platform_order_id,
        ).limit(2))).mappings().all()
    except IntegrationTablesUnavailable:
        return BypmsOrderDiagnosis(available=False, state="unavailable", next_action="同步记录暂不可用，请检查同步服务版本。")
    orders = (await db.execute(select(Order).where(
        Order.platform_order_id == platform_order_id,
    ).order_by(Order.order_id).limit(21))).scalars().all()
    active = [o for o in orders if not o.is_deleted]
    result = BypmsOrderDiagnosis(
        found_in_staging=bool(rows), order_ids=[o.order_id for o in active[:20]],
        state="not_found", next_action="请核对平台订单号及同步日期范围；未找到不代表上游没有该订单。",
    )
    if len(rows) > 1 or len(orders) > 20:
        result.state = "manual_review"
        result.reasons = ["匹配结果异常，不能自动认定订单关联。"]
        result.next_action = "请管理员核对订单关联，避免重复建单。"
        return result
    if rows:
        source = rows[0]
        result.fetched_at = _as_utc(source['fetched_at'])
        age = (now - result.fetched_at).total_seconds() if result.fetched_at else None
        result.staging_stale = age is None or age < -60 or age > _stale_after_seconds()
        if result.staging_stale:
            result.reasons.append("此订单的抓取记录已过期或时间异常，当前诊断不能代表上游最新状态。")
        if source['status'] == 'D' and not active:
            result.state = 'cancelled'
            result.next_action = '上游暂存记录为取消状态，不应直接补建；如有疑问请先核对上游。'
            return result
    if active:
        result.state = 'linked'
        result.next_action = '打开系统订单核对具体字段；已关联不等于所有字段均已同步。'
        if rows and rows[0]['status'] == 'D' and any(o.order_status != OrderStatus.cancelled for o in active):
            result.reasons.append('上游暂存记录已取消，但系统订单尚未全部取消，请核对取消同步及人工接管情况。')
        if len(active) > 1:
            result.reasons.append('同一平台订单号对应多个系统订单，需要人工核对。')
        for order in active:
            if locked_fields(order.metadata_):
                result.reasons.append('存在人工接管字段，自动同步会保留人工值。')
        conflicts = (await db.execute(select(OrderSyncConflict.field).where(
            OrderSyncConflict.source_order_id.in_([o.order_id for o in active]),
            OrderSyncConflict.status == OrderSyncConflictStatus.open,
        ).limit(1))).first()
        if conflicts:
            result.reasons.append('存在待处理同步差异，请在订单详情核对后处理。')
        if result.reasons:
            result.state = 'manual_review'
        if not rows:
            result.reasons.append('系统订单已存在，但当前暂存范围没有该平台订单，请核对历史记录。')
        return result
    if not rows:
        return result
    result.state = 'missing_order'
    result.next_action = '先核对候选订单和上游记录；确认无重复后，通过订单对账的受控流程处理。'
    source = rows[0]
    # Same-name/same-stay matches are only candidates, never proof of identity or a write authorization.
    if source['guest_name'] and source['check_in'] and source['check_out']:
        candidates = (await db.execute(select(Order).join(OrderRoom, OrderRoom.order_id == Order.order_id).where(
            Order.guest_name == source['guest_name'], Order.platform_order_id.is_(None),
            OrderRoom.check_in_date == source['check_in'], OrderRoom.check_out_date == source['check_out'],
        ).distinct().order_by(Order.order_id).limit(21))).scalars().all()
        result.candidate_order_ids = [o.order_id for o in candidates[:20] if not o.is_deleted]
        if candidates:
            result.state = 'manual_review'
            result.reasons.append('存在同名同期手录候选，身份尚未确认；不能直接补建。')
            if any(locked_fields(o.metadata_) for o in candidates):
                result.reasons.append('候选订单存在人工锁定，重试不会解除锁定。')
            if any(o.is_deleted or o.order_status == OrderStatus.cancelled for o in candidates):
                result.reasons.append('候选包含已删除或取消记录，需要管理员排除重复恢复风险。')
    if orders:
        result.state = 'manual_review'
        result.reasons.append('存在已删除的同平台单号订单，自动补建可能重复。')
    if not result.reasons:
        result.reasons.append('已抓到订单，但尚无平台单号关联；具体创建原因需结合最近同步步骤核对。')
    return result
