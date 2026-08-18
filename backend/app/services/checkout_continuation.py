"""退房防呆：探测「续住续单」（同房同客、退房日紧接着还有下一段订单）。

背景（1613 事故 2026-07-09）：携程续住常拆成两张独立订单。管家对第一张办退房会
撤销客人码、再对续住单办入住又发新码 → 客人拿旧码进不去。正确做法是对原单点
「门锁密码延期」沿用原密码（王总 2026-07-08 决策——手动、系统不自动判定，规避重名误配）。

本模块**只读、只提醒**：退房前探测本单是否有续住续单，供 CheckoutModal 弹提醒把
管家从「办退房」拉回「门锁密码延期」。绝不自动改门锁/订单（尊重"不自动判定"原则）。
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom

# 续住续单只可能落在这些「未走到退房」的态；completed/pending_checkout（已退/退房中）、
# cancelled/rescheduled/abnormal 都不算续住续单。
_UPCOMING_STATUSES = (
    OrderStatus.pending_confirm,
    OrderStatus.pending_payment,
    OrderStatus.paid_pending_room,
    OrderStatus.roomed_pending_checkin,
    OrderStatus.checked_in,
)

# 可关联状态：在 _UPCOMING 基础上再加「已退房/已完成」——用于「关联历史老单」，
# 让做完账/退了房的续住老单也能拴成一组（不含 cancelled/rescheduled/abnormal）。
# 退房提醒(checkout_precheck)仍只用 _UPCOMING；只有关联候选/link 才放宽到这一档。
_LINKABLE_STATUSES = _UPCOMING_STATUSES + (
    OrderStatus.pending_checkout,
    OrderStatus.completed,
)


async def find_checkout_continuations(db, order, statuses=_UPCOMING_STATUSES,
                                      require_same_name: bool = True) -> list[dict]:
    """本单每间房的退房日，是否紧接着有**同房**（默认再加**同客**）的续住续单？

    返回 [{room_id, next_order_id, guest_name, check_in_date(ISO)}]，每间房至多一条。

    require_same_name（口径开关，两个场景差异化）：
    - True（默认）= **退房防呆自动提醒**用：必须同名。保守，规避重名误报，客名不同不自动弹
      （王总 2026-07-08「不自动判定」原则）。同名用 func.trim(guest_name) 匹配（与建单去重
      同口径）——OTA 客名常带首尾空格，精确 == 会漏判，正是本功能要防的漏提醒。
    - False = **手动关联候选**用：放宽掉同名，只要同房+日期相连就列为候选（带出对方姓名供
      前台肉眼核对）。夫妻俩用两个名字各定一张续住单就是这么关联的。误配责任落在人点确认那步。
    """
    rooms = (await db.execute(
        select(OrderRoom).where(
            OrderRoom.order_id == order.order_id,
            OrderRoom.room_id.isnot(None),
        )
    )).scalars().all()
    return await _next_segments_per_room(
        db,
        frontiers=[(r.room_id, r.check_out_date) for r in rooms],
        exclude_order_ids=[order.order_id],
        guest_name=order.guest_name,
        statuses=statuses,
        require_same_name=require_same_name,
    )


async def find_group_continuations(db, order, statuses=_LINKABLE_STATUSES,
                                   require_same_name: bool = False) -> list[dict]:
    """本单所在**续住组**（未成组 = 本单自己）里，每间房各自的续段候选，每间房至多一条。

    与 find_checkout_continuations 的差别只在**从哪儿往后找**：

    - 旧口径（`resolve_link_base`，#339）：把整组收敛成「退房日最晚的那一张单」，
      再从那张单的房间出发。
    - 本函数：按**房间**逐间取组内该房的最晚退房日出发。

    为什么必须按房间（生产事故 程鹏 2026-07-26）：携程「同客多间」是**一张单挂多间房**
    （ORD-20260723-0A1A = 1608+1609）。合并掉 1608 那条腿之后，组末段是只含 1608 的那张
    续段单，旧口径从它出发就再也够不着 1609 的续段 —— 前台「1609 合并不了」。更糟的是
    从只剩 1608 的末段往后找会命中 1608 的**下一位客人**，一点关联就把陌生人拴进本组
    （= 一个门锁密码发给两拨客人）。

    按房间走同时保留 #339 的扩段能力：某间房已经续到第 N 段，该房的出发点自然就是第 N 段
    的退房日，N 段可无限续接（沈桑场景）。未成组时组 = 本单，行为与旧口径逐字节一致。
    """
    members = await _alive_group_members(db, order)
    member_ids = [m.order_id for m in members]
    frontiers = (await db.execute(
        select(OrderRoom.room_id, func.max(OrderRoom.check_out_date))
        .where(OrderRoom.order_id.in_(member_ids), OrderRoom.room_id.isnot(None))
        .group_by(OrderRoom.room_id)
        .order_by(OrderRoom.room_id)      # 候选顺序确定，避免前端「第一条」漂移
    )).all()
    return await _next_segments_per_room(
        db,
        frontiers=[(rid, co) for rid, co in frontiers],
        exclude_order_ids=member_ids,
        guest_name=order.guest_name,
        statuses=statuses,
        require_same_name=require_same_name,
    )


async def _alive_group_members(db, order) -> list[Order]:
    """组内活段（未成组 = [本单]）。

    排除已取消段，与 stay_group 的 alive_only 同口径：取消段若退房日最晚，会把某间房的
    出发点推到一个客人根本不会来的日期，真候选就此消失。
    """
    if not order.stay_group_id:
        return [order]
    rows = (await db.execute(
        select(Order).where(
            Order.stay_group_id == order.stay_group_id,
            Order.is_deleted.is_(False),
            Order.order_status != OrderStatus.cancelled,
        )
    )).scalars().all()
    return list(rows) or [order]


async def _next_segments_per_room(db, *, frontiers, exclude_order_ids, guest_name,
                                  statuses, require_same_name: bool) -> list[dict]:
    """「紧接着的下一段」的单一定义：同房 + 入住日 == 出发日 + 状态可关联（+ 可选同客）。

    frontiers = [(room_id, 出发日)]；每间房至多返回一条。两个调用方（单张单 / 整组）只是
    出发日算法不同，匹配口径必须同源，否则「看着像续住」提示、弹窗候选、实际关联会各说各话。
    """
    guest = (guest_name or "").strip()
    out: list[dict] = []
    for room_id, frontier in frontiers:
        conds = [
            OrderRoom.room_id == room_id,
            OrderRoom.check_in_date == frontier,          # 首尾相连
            Order.order_id.notin_(exclude_order_ids),
            Order.is_deleted.is_(False),
            Order.order_status.in_(statuses),
        ]
        if require_same_name:
            conds.append(func.trim(Order.guest_name) == guest)  # 同客（去空格，与建单去重同口径）
        nxt = (await db.execute(
            select(Order.order_id, Order.guest_name, OrderRoom.check_in_date)
            .join(OrderRoom, OrderRoom.order_id == Order.order_id)
            .where(*conds)
            .order_by(OrderRoom.check_in_date, Order.order_id)
            .limit(1)
        )).first()
        if nxt:
            out.append({
                "room_id": room_id,
                "next_order_id": nxt.order_id,
                "guest_name": nxt.guest_name,
                "check_in_date": nxt.check_in_date.isoformat(),
            })
    return out
