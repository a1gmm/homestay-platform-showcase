"""订单状态机 + 房态联动的唯一实现（2026-07-08 架构扫描 #183 收敛）。

历史问题：状态流转逻辑散在 orders.py（正门 _apply_order_transition）、
staff_portal.py（办入住手动改状态 + 复制房态联动 + 跨文件偷 import 私有函数）、
workers/order_maintenance.py（自动取消手动改状态）三处，改一处漏两处。

本模块之后的规矩：
- 沿状态图走的流转 → apply_order_transition()
- 员工端 walk-in「快进入住」（允许从更早状态直达 checked_in，是有意的
  业务快路径不是漏洞）→ fast_checkin()
- 任何调用方不准再手写 order.order_status = ... 或自摆房态。

门锁下码/撤码 hook 不在本模块：它们必须在 commit 之后跑（fail-safe，
门锁故障不回滚状态），由各 endpoint/worker 在 commit 后自行调用。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from app.core.datetime_helpers import now_cn
from app.models.order import DepositStatus, Order, OrderStatus, order_status_label
from app.models.order_room import OrderRoom
from app.models.room import Room, RoomStatus


async def mark_room_reserved_if_available(db, room_id: str) -> None:
    """房态联动：room 当前 available → 改 reserved。不覆盖 occupied / cleaning 等真实态。"""
    room = (await db.execute(select(Room).where(Room.room_id == room_id))).scalar_one_or_none()
    if room and room.room_status == RoomStatus.available:
        room.room_status = RoomStatus.reserved


async def release_reserved_room(db, room_id: str) -> None:
    """房态联动：room 当前 reserved → 改回 available。其它态不动（occupied/cleaning 等是真实态）。"""
    room = (await db.execute(select(Room).where(Room.room_id == room_id))).scalar_one_or_none()
    if room and room.room_status == RoomStatus.reserved:
        room.room_status = RoomStatus.available


async def order_room_ids(db, order: Order) -> list[str]:
    """返回订单关联的全部已分配房间 id（多房间订单的真相源）。

    直接查 order_rooms 表，避免在未 selectinload 的 async 上下文里触发 lazy load。
    回退到 deprecated 顶层 order.room_id 兜老数据。
    """
    rows = (await db.execute(
        select(OrderRoom.room_id).where(
            OrderRoom.order_id == order.order_id,
            OrderRoom.room_id.isnot(None),
        )
    )).scalars().all()
    ids = list(dict.fromkeys(rid for rid in rows if rid))
    if not ids and order.room_id:
        ids = [order.room_id]
    return ids


async def active_order_room_ids(db, order: Order) -> list[str]:
    """返回订单里「尚未退房」的已排房 room_id（per-room checkout 用）。

    checked_out_at IS NULL 即在住。多房单逐间退房时，这个集合会随退房递减；
    集合空了（全部房都已 checked_out）代表整单可转 pending_checkout。
    与 order_room_ids 区别：后者返回全部已排房（含已退的），画甘特/历史用。
    回退到 deprecated 顶层 order.room_id 兜老数据（老数据无 order_rooms 行时视为在住）。
    """
    rows = (await db.execute(
        select(OrderRoom.room_id).where(
            OrderRoom.order_id == order.order_id,
            OrderRoom.room_id.isnot(None),
            OrderRoom.checked_out_at.is_(None),
        )
    )).scalars().all()
    ids = list(dict.fromkeys(rid for rid in rows if rid))
    if not ids and order.room_id:
        # 有 order_rooms 行但全已退 → 不兜底；仅当完全没有 order_rooms 行才回退老字段。
        has_rows = (await db.execute(
            select(OrderRoom.order_room_id).where(OrderRoom.order_id == order.order_id)
        )).first() is not None
        if not has_rows:
            ids = [order.room_id]
    return ids


# Forward-only lifecycle with one explicit "rescheduling" side loop.
# Terminal states (completed / cancelled) have no outgoing edges. Going backward in the
# main flow (un-paying, un-checking-in, re-opening a completed order) is prohibited —
# operators must cancel + recreate, or use dedicated corrective endpoints (refund, room
# reassignment) which do not change order_status. The only intentional loop is
# roomed_pending_checkin ⇄ rescheduled: customer-initiated reschedules route through
# the rescheduled state to be re-assigned back to roomed_pending_checkin with new dates.
# Every transition is persisted to audit_log, which provides a queryable timeline —
# no separate transitioned_at column is needed.
# 2026-06-05 王总流程调整：把「确认收款」从下单后第 2 步挪到客人退房之后。
# 实际业务里房费常是入住后/离店第二天（平台代收离店后结算）才到账，所以收款确认
# 放到流程最后。新顺序：确认订单 → 确认排房 → 办理入住 → 发起退房 → 确认收款 → 完成订单。
#
# ⚠️ 枚举值复用、语义重定义：
#   pending_payment 由原「待支付（早期态）」重定义为「待完成（后期态，已退房+已确认收款）」。
#   配套的一次性数据迁移把存量早期 pending_payment 单迁到 paid_pending_room（见 alembic
#   迁移 c1d2..→ 新 revision）。改动后 pending_payment 只会出现在「确认收款 → 完成订单」之间。
#   pending_checkout 文案改为「已退房待收款」，paid_pending_room 文案改为「待排房」。
VALID_TRANSITIONS = {
    OrderStatus.pending_confirm: [
        OrderStatus.paid_pending_room,  # 确认订单 → 待排房（收款不再在此处）
        OrderStatus.cancelled,
    ],
    OrderStatus.paid_pending_room: [
        OrderStatus.roomed_pending_checkin,  # 确认排房
        OrderStatus.cancelled,
    ],
    OrderStatus.roomed_pending_checkin: [
        OrderStatus.checked_in,  # 办理入住
        OrderStatus.rescheduled,
        OrderStatus.cancelled,
    ],
    OrderStatus.checked_in: [
        OrderStatus.pending_checkout,  # 发起退房
        OrderStatus.abnormal,
    ],
    OrderStatus.pending_checkout: [
        # 2026-06-27 王总：砍掉「确认收款」中间步。退房后可一步直接「完成订单」
        # （收齐校验仍在 apply_order_transition→completed 收口）。
        OrderStatus.completed,  # 完成订单（此处校验房费收齐）
        OrderStatus.pending_payment,  # 保留旧边：兼容存量在途单，前端已不再提供该入口
        OrderStatus.abnormal,
    ],
    OrderStatus.pending_payment: [
        OrderStatus.completed,  # 完成订单（存量「待完成」单的出口；此处校验房费收齐）
        OrderStatus.abnormal,
    ],
    OrderStatus.rescheduled: [
        OrderStatus.roomed_pending_checkin,
        OrderStatus.cancelled,
    ],
    OrderStatus.abnormal: [
        OrderStatus.completed,
        OrderStatus.cancelled,
    ],
    # Terminal states — no outgoing transitions.
    OrderStatus.completed: [],
    OrderStatus.cancelled: [],
}


async def _sync_rooms_for_status(db, order: Order, target_status: OrderStatus) -> None:
    """房态联动 — 所有入口共用。多房间订单:每间房都要联动,不能只动 order.room_id 这一间 (#42)。"""
    for rid in await order_room_ids(db, order):
        room = (await db.execute(select(Room).where(Room.room_id == rid))).scalar_one_or_none()
        if not room:
            continue
        if target_status == OrderStatus.cancelled and room.room_status == RoomStatus.reserved:
            room.room_status = RoomStatus.available
        elif target_status == OrderStatus.checked_in:
            room.room_status = RoomStatus.occupied
        elif target_status == OrderStatus.completed:
            # 退房 → 完成：清扫完成，房间释放
            if room.room_status in (RoomStatus.pending_clean, RoomStatus.cleaning, RoomStatus.occupied):
                room.room_status = RoomStatus.available


async def _stamp_rooms_checked_in(db, order: Order) -> None:
    """给该单所有已排房、尚未入住的 OrderRoom 行盖上 checked_in_at（per-room 真相源）。

    整单入住 = 全部一起入住；已入住行不重复覆盖时刻。整单/单间/通用流转三条入口
    共用，保证任何进入 checked_in 的路径都写 checked_in_at，否则前端「办理入住（还剩
    N 间）」会把 checked_in_at 为空的房误算成"还没入住"。
    """
    checkin_now = now_cn()
    rooms = (await db.execute(
        select(OrderRoom).where(OrderRoom.order_id == order.order_id)
    )).scalars().all()
    for r in rooms:
        if r.room_id and r.checked_in_at is None:
            r.checked_in_at = checkin_now


async def apply_order_transition(db, order: Order, target_status: OrderStatus) -> str:
    """统一的订单状态流转：流转图校验 + 守卫 + 房态联动 + 写入状态。

    所有入口（单笔 /transition、批量 /batch/transition、worker 自动取消）都必须
    经此函数，避免某个入口漏掉守卫或副作用（#40）。守卫不满足时抛 HTTPException。
    返回流转前的状态字符串（供审计 diff）。
    """
    allowed = VALID_TRANSITIONS.get(order.order_status, [])
    if target_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"状态不允许从「{order_status_label(order.order_status)}」流转到「{order_status_label(target_status)}」",
        )
    # 走到 cancelled 时也走 cancel 一样的押金校验，保证两条 endpoint 不出歧义。
    if target_status == OrderStatus.cancelled and order.deposit_status == DepositStatus.collected:
        raise HTTPException(
            status_code=400,
            detail="押金已收取，请先调用 /deposit/return 或 /deposit/withhold 处理后再取消订单",
        )
    # 2026-06-27 王总：取消「完成订单必须房费收齐」卡点。走携程等平台不存在欠款，
    # 强制记录收款才能完成是多余负担。完成订单不再校验房费是否收齐。
    # 注：营收/结算报表按 Order.actual_price 计（见 dashboard.py / settlements.py），
    # 不依赖收款记录，故取消此卡点不影响财务数字。押金收/退仍走独立路径，不受影响。

    before_status = order.order_status.value
    order.order_status = target_status

    # 确认订单时自动跳过「待排房」：若该单所有房间行都已排房（如甘特图上建单即带房、
    # 平台单预排房），直接续推到「待入住」，免去对已排好房订单还要人工点一次「确认排房」。
    # 仅当存在未排房行（OrderRoom.room_id IS NULL）时才停在「待排房」等人工排房。
    # paid_pending_room 仅由 pending_confirm 进入（见 VALID_TRANSITIONS），故此判断口径唯一；
    # roomed_pending_checkin 与 paid_pending_room 的房态联动一致（均不改房态），续推安全。
    if target_status == OrderStatus.paid_pending_room:
        room_rows = (await db.execute(
            select(OrderRoom.room_id).where(OrderRoom.order_id == order.order_id)
        )).scalars().all()
        fully_assigned = (
            all(rid is not None for rid in room_rows) if room_rows
            else order.room_id is not None
        )
        if fully_assigned:
            target_status = OrderStatus.roomed_pending_checkin
            order.order_status = target_status

    await _sync_rooms_for_status(db, order, target_status)
    # 进入「在住」的通用流转（后台弹窗单房无押金直接 transition、批量流转、撤销退房级联等）
    # 也要盖 checked_in_at，与 fast_checkin / checkin_one_room 口径一致，避免整单已在住却
    # 所有房 checked_in_at 为空 → 界面误报「办理入住（还剩 N 间）」。
    if target_status == OrderStatus.checked_in:
        await _stamp_rooms_checked_in(db, order)
    return before_status


async def complete_checkout_order(db, order_id: str | None) -> bool:
    """退房收尾：把 pending_checkout 订单一步推到 completed，释放房间。幂等。

    仅当订单存在、未删除、且仍处 pending_checkout 时推进（走 apply_order_transition
    正门 → 房态联动把房释放）。已完成/已取消/其它态或订单不存在 → 直接跳过返回 False，
    不抛异常。调用方（飞书退押金卡「查房+退押金」双完成）best-effort 触发，
    不落库校验状态由本函数收口，避免调用点各自判断口径漂移。返回是否真的推进了。
    """
    if not order_id:
        return False
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if order is None or order.order_status != OrderStatus.pending_checkout:
        return False
    await apply_order_transition(db, order, OrderStatus.completed)
    return True


def _guard_pre_checkin_state(order: Order) -> None:
    """办理入住前置态守卫：末态/已退房/已取消不能再入住（#41）。整单与单间入住共用。"""
    if order.order_status in (
        OrderStatus.pending_checkout, OrderStatus.pending_payment, OrderStatus.completed,
    ):
        raise HTTPException(status_code=400, detail=f"订单已{order.order_status.value},无需再办理入住")
    if order.order_status == OrderStatus.cancelled:
        raise HTTPException(status_code=400, detail="订单已取消")


async def fast_checkin(db, order: Order) -> None:
    """员工端 walk-in 快进入住（整单）：允许从 pending_confirm / paid_pending_room /
    roomed_pending_checkin / rescheduled / abnormal 直达 checked_in。

    这是有意的业务快路径（现场客人到店，前台一步办完），不是绕过状态机的漏洞。
    已入住及之后的状态、已取消 → 400。
    （pending_payment 自 2026-06-05 起语义为「已退房·待完成」,是退房后的末态,
    不能再办理入住 #41。）
    整单入住会把所有已排房、尚未入住的 OrderRoom 行一并打上 checked_in_at（per-room 真相源
    与单间入住一致），已入住行不重复覆盖时刻。
    """
    if order.order_status == OrderStatus.checked_in:
        raise HTTPException(status_code=400, detail=f"订单已{order.order_status.value},无需再办理入住")
    _guard_pre_checkin_state(order)

    order.order_status = OrderStatus.checked_in
    await _sync_rooms_for_status(db, order, OrderStatus.checked_in)
    # 给所有已排房、未入住的房行打入住时刻（整单入住 = 全部一起入住）。
    await _stamp_rooms_checked_in(db, order)


async def checkin_one_room(db, order: Order, target: OrderRoom) -> None:
    """单间入住（per-room checkin，镜像单间退房）：只给 target 这一间办理入住。

    首间入住时订单由入住前态转 checked_in；订单已 checked_in（多房逐间入住）则保持。
    只把 target 房置 occupied、打 checked_in_at，不碰订单里其它未入住的房（保持 reserved）。
    校验由调用方（handle_checkin）做：target 属本单、已排房、未入住。
    """
    # 订单尚未入住 → 走前置态守卫并转 checked_in；已入住则保持（逐间入住第 2..N 间）。
    if order.order_status != OrderStatus.checked_in:
        _guard_pre_checkin_state(order)
        order.order_status = OrderStatus.checked_in

    target.checked_in_at = now_cn()
    if target.room_id:
        room = (await db.execute(
            select(Room).where(Room.room_id == target.room_id)
        )).scalar_one_or_none()
        # 门闩已移除（2026-07-09）：入住无条件置 occupied，覆盖 pending_clean/cleaning。
        # 维修/锁房态是真实态，不覆盖（换房旧房场景）。
        if room and room.room_status not in (RoomStatus.maintenance, RoomStatus.locked):
            room.room_status = RoomStatus.occupied
