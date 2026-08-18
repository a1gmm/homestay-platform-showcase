"""
Guest auto-linking service.
Called on order creation to find-or-create a guest record
and update aggregated stats.
"""
import re
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal
from datetime import datetime, timezone
import uuid

from app.models.guest import Guest
from app.models.order import Order, StaySettlementKind


def normalize_phone(raw: str | None) -> str:
    """规范化手机号：去掉所有空白/标点/+86 前缀，留 11 位本地形式。
    避免「+86 130 1234 5678」「(86) 13012345678」「86-130-1234-5678」 各产生独立 guest 记录。"""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


async def link_guest_to_order(db: AsyncSession, order: Order) -> Guest | None:
    """
    Find guest by phone or create a new one.
    Increment visit_count, update total_spent, total_nights, last_check_in.

    issue#3: 手机号改为选填后，phone 为空时不自动建客人档案——
    避免前台录单时没电话的订单全归到一个空 phone 的"匿名客人"档案。
    返回 None 表示跳过自动建档；后续等前台补完电话后可手动 link。
    """
    if not order.guest_phone:
        return None
    normalized = normalize_phone(order.guest_phone)
    if not normalized:
        # phone 字段被填了但全是非数字字符 → 也跳过建档
        return None
    if normalized != order.guest_phone:
        # 顺手把订单上的 phone 也标准化，让后续报表/查询一致
        order.guest_phone = normalized
    result = await db.execute(
        select(Guest).where(Guest.phone == normalized)
    )
    guest = result.scalar_one_or_none()

    if not guest:
        guest = Guest(
            guest_id="GST-" + uuid.uuid4().hex[:12].upper(),
            name=order.guest_name,
            phone=normalized,
            visit_count=0,
            total_spent=Decimal("0"),
            total_nights=0,
        )
        db.add(guest)

    # Update stats
    guest.visit_count += 1
    # 公司承担段不是客人消费；即使历史脏数据误填 actual_price，也不得污染客人画像。
    guest.total_spent += (
        Decimal("0")
        if order.stay_settlement_kind == StaySettlementKind.company_sponsored
        else order.actual_price or Decimal("0")
    )
    guest.total_nights += order.nights
    guest.last_check_in = datetime.now(timezone.utc)
    if order.room_id:
        guest.preferred_room = order.room_id

    # Update name if changed
    if order.guest_name and order.guest_name != guest.name:
        guest.name = order.guest_name

    # Auto-tag repeat guests
    if guest.visit_count > 1 and guest.tags:
        if "回头客" not in guest.tags:
            guest.tags = guest.tags + ",回头客"
    elif guest.visit_count > 1:
        guest.tags = "回头客"

    return guest
