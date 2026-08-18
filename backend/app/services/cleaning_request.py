"""飞书「打扫申请群」自助保洁 —— 服务层。

保洁在飞书群自助申请打扫、拿门锁码、点「打扫完了」撤码；前台/管家点「通过」计费。
计费复用续住打扫（30，房东全担），只记账绝不碰房态（房里还住着人）。
详见 PRD：~/.claude/plans/打扫申请群-飞书自助-PRD-2026-07-12.md
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cleaning_pricing import INSTAY_CLEANING_FEE
from app.core.config import settings
from app.core.datetime_helpers import today_cn
from app.core.trial_tags import trial_tag_for_notes
from app.models.cleaning_request import (
    CleaningApprovalStatus,
    CleaningRequest,
    CleaningRequestStatus,
)
from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.managed_stay_group import ManagedStayGroup, ManagedStayGroupKind
from app.models.room import Room


async def _collect_active_stays(
    db: AsyncSession,
    statuses: tuple[OrderStatus, ...] = (OrderStatus.checked_in,),
) -> tuple[dict[str, Order], list[tuple[str, str, date | None, date | None]]]:
    """收集指定状态（未删）订单的「房-住」单元 (room_id, order_id, ci, co)。

    OrderRoom 优先（多房/换房拆单各自日期），无行的订单回退单字段（老单一房）。
    返回 (order_by_id, stays)；stays 未做日期过滤，供续住/退房各口径复用。

    statuses 默认只收 checked_in（续住卡口径）；退房卡额外收 pending_checkout——
    客人一进退房流程状态就从 checked_in 变 pending_checkout，只扫 checked_in 会把
    今天正在退房的房从「今日退房房」提醒里漏掉（生产 1416/1609）。
    """
    orders = (
        (
            await db.execute(
                select(Order).where(
                    Order.order_status.in_(statuses),
                    Order.is_deleted == False,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    if not orders:
        return {}, []

    order_by_id = {o.order_id: o for o in orders}
    rows = (
        (
            await db.execute(
                select(OrderRoom).where(OrderRoom.order_id.in_(list(order_by_id)))
            )
        )
        .scalars()
        .all()
    )

    orders_with_rooms: set[str] = set()
    stays: list[tuple[str, str, date | None, date | None]] = []
    for r in rows:
        if r.room_id is None:  # 待排房，跳过
            continue
        orders_with_rooms.add(r.order_id)
        stays.append((r.room_id, r.order_id, r.check_in_date, r.check_out_date))
    for o in orders:
        if o.order_id in orders_with_rooms:
            continue
        if o.room_id is None:
            continue
        stays.append((o.room_id, o.order_id, o.check_in_date, o.check_out_date))
    return order_by_id, stays


async def _rooms_dicts_from_kept(
    db: AsyncSession,
    order_by_id: dict[str, Order],
    kept: dict[tuple[str, str], tuple[date, date]],
) -> list[dict]:
    """把 (room, order)->(ci, co) 组装成带房名/客人名的行，按 room_id 排序。"""
    if not kept:
        return []

    room_ids = {room_id for room_id, _ in kept}
    room_names = dict(
        (
            await db.execute(
                select(Room.room_id, Room.room_name).where(Room.room_id.in_(room_ids))
            )
        ).all()
    )

    result = [
        {
            "room_id": room_id,
            "room_name": room_names.get(room_id, room_id),
            "order_id": order_id,
            "guest_name": order_by_id[order_id].guest_name,
            "check_in_date": ci,
            "check_out_date": co,
            # 试运营房型（极海）角标：备注命中关键词时打标，与房态页同源。大卡逐行渲染。
            "trial_tag": trial_tag_for_notes(order_by_id[order_id].notes),
        }
        for (room_id, order_id), (ci, co) in kept.items()
    ]
    result.sort(key=lambda x: x["room_id"])
    return result


async def _continuation_group_units(
    db: AsyncSession,
) -> tuple[dict[str, Order], list[tuple[str, str, str, date, date]]]:
    """续住组补收口径：组内有 checked_in 成员时，把每个房的「整组日历跨度」
    (room_id, anchor_order_id, ending_order_id, union_ci, union_co) 收出来。

    两个 order_id 各有用途：
    - anchor_order_id = 组内含此房的 **checked_in 成员**，续住卡挂它（过
      room_belongs_to_checked_in_order 申请闸）。
    - ending_order_id = 此房**退房日=组末**的那段（可能是 pending_confirm/pending_checkout），
      退房卡挂它（纯展示、不过闸，语义上就是退房那张单）。

    根因（生产 2026-07-29）：OTA 续住后续段进来挂 pending_confirm（link_continuation
    只设 stay_group_id 不改状态），而 _collect_active_stays 只扫 checked_in、按该单
    自己腿日期过滤——锚点段日期一过、当前段挂 pending_confirm，房就从续住卡+退房卡
    上同时消失，保洁没按钮可点（王艳1602/程鹏1608/1609/1610/秦俊贤1616/吕军1411）。

    这里绕开单段状态，按**整组**算：合并组内所有活着的段(排除 cancelled/deleted)得每房
    日历跨度；order_id 归组内**含此房的 checked_in 成员**，使 room_belongs_to_checked_in_order
    放行。上层用整组跨度做同一套日期过滤（在店/退房），天然对称、不重不漏。
    """
    grouped = (
        await db.execute(
            select(Order).where(
                Order.is_deleted == False,  # noqa: E712
                Order.stay_group_id.isnot(None),
                Order.order_status != OrderStatus.cancelled,
                ~exists().where(
                    ManagedStayGroup.stay_group_id == Order.stay_group_id,
                    ManagedStayGroup.kind == ManagedStayGroupKind.managed_split,
                ),
            )
        )
    ).scalars().all()
    if not grouped:
        return {}, []

    by_group: dict[str, list[Order]] = {}
    for o in grouped:
        by_group.setdefault(o.stay_group_id, []).append(o)

    or_rows = (
        await db.execute(
            select(OrderRoom).where(
                OrderRoom.order_id.in_([o.order_id for o in grouped])
            )
        )
    ).scalars().all()
    rooms_by_order: dict[str, list[tuple[str, date | None, date | None]]] = {}
    for r in or_rows:
        if r.room_id is None:  # 待排房腿跳过
            continue
        rooms_by_order.setdefault(r.order_id, []).append(
            (r.room_id, r.check_in_date, r.check_out_date)
        )

    def _units(o: Order) -> list[tuple[str, date | None, date | None]]:
        # OrderRoom 优先（多房/换房各自日期），无行回退单字段（老单一房）。
        if o.order_id in rooms_by_order:
            return rooms_by_order[o.order_id]
        if o.room_id is not None:
            return [(o.room_id, o.check_in_date, o.check_out_date)]
        return []

    order_ref: dict[str, Order] = {}
    result: list[tuple[str, str, str, date, date]] = []
    for members in by_group.values():
        checked_in = [m for m in members if m.order_status == OrderStatus.checked_in]
        if not checked_in:
            continue  # 组里还没人真入住 → 不收（gate 也会拒）
        by_oid = {m.order_id: m for m in members}
        # 每房归到「含此房的 checked_in 成员」，供 gate 放行（多房锚点覆盖各房）。
        room_anchor: dict[str, str] = {}
        for m in checked_in:
            for (rid, _c, _o) in _units(m):
                room_anchor.setdefault(rid, m.order_id)
        # 每房整组日历跨度 = 组内该房所有段的 min(ci)~max(co)；同时记退房日=组末那段。
        span: dict[str, tuple[date, date]] = {}
        ending: dict[str, str] = {}
        for m in members:
            for (rid, ci, co) in _units(m):
                if ci is None or co is None:
                    continue
                if rid in span:
                    lo, hi = span[rid]
                    span[rid] = (min(lo, ci), max(hi, co))
                    if co >= hi:  # 更晚（或并列最晚）的退房段 → 记为退房那段
                        ending[rid] = m.order_id
                else:
                    span[rid] = (ci, co)
                    ending[rid] = m.order_id
        for rid, (ci, co) in span.items():
            anchor_oid = room_anchor.get(rid)
            if not anchor_oid:
                continue  # 该房无 checked_in 成员覆盖 → gate 会拒，跳过
            ending_oid = ending.get(rid, anchor_oid)
            result.append((rid, anchor_oid, ending_oid, ci, co))
            order_ref[anchor_oid] = by_oid[anchor_oid]
            order_ref[ending_oid] = by_oid[ending_oid]
    return order_ref, result


async def list_instay_rooms_for_cleaning(
    db: AsyncSession, *, ref_date: date | None = None
) -> list[dict]:
    """列「续住打扫」的在住房：昨天及以前入住、今天还没走、还继续住。

    口径（PRD 三.1）：order checked_in（未删）且**该房自身**
    check_in_date < ref_date 且 check_out_date > ref_date；
    **外加**续住组边界日——该房本段 check_out_date == ref_date 但客人续住到下一段
    （非组末段）时也算续住（人今晚还住着，房该按续住打扫一次；退房卡对称排除中间段）。
    排除今日新入住、今天真要退房走的（组末段）、已退房的。

    房日期以 OrderRoom 为准（多房/换房拆单各自日期）；订单无 OrderRoom 行时
    回退 orders 单字段（老单一房），与续住计费端点同源双读，避免漏/重。

    返回按 room_id 排序的 [{room_id, room_name, order_id, guest_name,
    check_in_date, check_out_date}]，每物理房一条。
    """
    from app.services import stay_group as _stay_group

    ref = ref_date or today_cn()
    order_by_id, stays = await _collect_active_stays(db)

    # 续住组（有 checked_in 锚点）的房：整组日历跨度是**唯一真相**，先算出来。逐段口径对
    # 多房组的 is_group_last 有 quirk（同组两间同日退房只认一间为末段），会把今天要走的房
    # 误判成在店 → 与整组口径打架、同房上两张卡。故这些房从逐段口径摘掉，统一由整组跨度裁。
    cont_orders, cont = await _continuation_group_units(db)
    group_rooms = {rid for (rid, _a, _e, _ci, _co) in cont}

    # 逐段口径：非续住组的普通在住房。入住早于今天、退房晚于今天；携带匹配段自身 ci/co
    # 避免二次回扫误取旧段日期；按 (room, order) 去重，同房多段只留一条。
    kept: dict[tuple[str, str], tuple[date, date]] = {}
    for room_id, order_id, ci, co in stays:
        if ci is None or co is None or room_id in group_rooms:
            continue
        if ci < ref < co:
            kept.setdefault((room_id, order_id), (ci, co))
        elif co == ref and ci < co:
            # 边界日：本段退房日=今天但客人续住到下一段（非组末段）→ 今晚还住着，算续住。
            order = order_by_id.get(order_id)
            if (order is not None and order.stay_group_id
                    and not await _stay_group.is_group_last_segment(db, order)):
                kept.setdefault((room_id, order_id), (ci, co))

    # 续住组房：整组跨度覆盖今天（今天退房走的归退房卡）→ 续住打扫。挂 checked_in 锚点
    # （过 room_belongs_to_checked_in_order 闸）。修 OTA 后续段挂 pending_confirm 致漏房。
    for rid, anchor_oid, _ending_oid, ci, co in cont:
        if ci is None or co is None:
            continue
        if ci < ref < co:
            kept.setdefault((rid, anchor_oid), (ci, co))
            order_by_id.setdefault(anchor_oid, cont_orders[anchor_oid])
    return await _rooms_dicts_from_kept(db, order_by_id, kept)


async def list_checkout_rooms_for_cleaning(
    db: AsyncSession, *, ref_date: date | None = None
) -> list[dict]:
    """列「今日退房房」：在住（checked_in）且该房今天（ref）退房走 —— check_out_date == ref。

    **纯展示用**（每日打扫卡上让保洁一眼看到今天还要打扫的退房房）。这些房的门锁码/
    计费仍走退房流程（查房通过计 65），本函数绝不参与自助申请/计费，避免与续住(30)串味。
    过滤掉 0 晚(ci==co)脏单。返回结构同 list_instay_rooms_for_cleaning，按 room_id 排序。

    续住组中间段（退房日=今天但客人去下一段续住）排除，客人不走、打扫走末段退房。
    与飞书退房提醒(reminder_tasks)/Dashboard 今日待退房同口径（is_group_last_segment）。
    """
    from app.services import stay_group as _stay_group

    ref = ref_date or today_cn()
    # 退房卡额外收 pending_checkout：客人一进退房流程就不再是 checked_in（生产 1416/1609）。
    order_by_id, stays = await _collect_active_stays(
        db, statuses=(OrderStatus.checked_in, OrderStatus.pending_checkout)
    )

    # 续住组（有 checked_in 锚点）的房整组跨度是唯一真相（与在店卡同源，防两卡打架/同房双卡）。
    cont_orders, cont = await _continuation_group_units(db)
    group_rooms = {rid for (rid, _a, _e, _ci, _co) in cont}

    kept: dict[tuple[str, str], tuple[date, date]] = {}
    for room_id, order_id, ci, co in stays:
        if room_id in group_rooms:
            continue  # 交给下面整组跨度统一裁
        if ci is not None and co is not None and co == ref and ci < co:
            order = order_by_id.get(order_id)
            if (order is not None and order.stay_group_id
                    and not await _stay_group.is_group_last_segment(db, order)):
                continue  # 续住中间段：今天退房日只是段界，客人续住不走
            kept.setdefault((room_id, order_id), (ci, co))

    # 续住组房：整组跨度末端=今天 → 组末退房日，客人今天真走 → 进退房卡（纯展示，挂退房那段）。
    # 修 OTA 后续段挂 pending_confirm 致漏房。
    for rid, _anchor_oid, ending_oid, ci, co in cont:
        if ci is None or co is None:
            continue
        if co == ref and ci < co:
            kept.setdefault((rid, ending_oid), (ci, co))
            order_by_id.setdefault(ending_oid, cont_orders[ending_oid])
    return await _rooms_dicts_from_kept(db, order_by_id, kept)


def is_cleaning_approver(open_id: str) -> bool:
    """点【通过】的飞书用户是否有审批权（纵深防御，主防线是「审核群只有管家」）。

    白名单来自 settings.FEISHU_CLEANING_APPROVER_OPEN_IDS（逗号分隔）。
    **空 = 不额外限制**（放行——【通过】按钮只在审核群出现，保洁不在此群点不到）；
    非空则严格白名单（王总想再收一道口子时配）。
    """
    raw = (settings.FEISHU_CLEANING_APPROVER_OPEN_IDS or "").strip()
    if not raw:
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return bool(open_id) and open_id in allowed


async def applied_room_ids_for_date(
    db: AsyncSession, *, ref_date: date | None = None
) -> set[str]:
    """今日已申请打扫的房间 id 集合（每日大卡原地更新时标「已申请」用）。"""
    ref = ref_date or today_cn()
    rows = (
        await db.execute(
            select(CleaningRequest.room_id).where(CleaningRequest.request_date == ref)
        )
    ).scalars().all()
    return set(rows)


# 每日大卡的飞书 message_id 存 notification_logs（免新迁移，同 daily_guard 手法），
# 供保洁申请后原地更新那张大卡（把已申请的房标灰）。按 CN 日 key，跨会话可取。
_DAILY_CARD_TEMPLATE = "cleaning-request-daily-card"


async def save_daily_card_message_id(
    db: AsyncSession, message_id: str, *, ref_date: date | None = None
) -> None:
    """记下今天大卡的 message_id（beat 发卡后调用）。同日重发先删旧再存。"""
    if not message_id:
        return
    from uuid import uuid4 as _uuid4

    from app.models.notification import NotificationLog

    ref = (ref_date or today_cn()).isoformat()
    await db.execute(
        NotificationLog.__table__.delete().where(
            NotificationLog.template_name == _DAILY_CARD_TEMPLATE,
            NotificationLog.content == ref,
        )
    )
    db.add(NotificationLog(
        log_id="CRC-" + _uuid4().hex[:12].upper(),
        template_name=_DAILY_CARD_TEMPLATE,
        content=ref,
        recipient=message_id,
        status="card",
    ))
    await db.commit()


async def get_daily_card_message_id(
    db: AsyncSession, *, ref_date: date | None = None
) -> str | None:
    """取今天大卡的 message_id（保洁申请后原地更新那张卡用）。无则 None。"""
    from app.models.notification import NotificationLog

    ref = (ref_date or today_cn()).isoformat()
    return await db.scalar(
        select(NotificationLog.recipient).where(
            NotificationLog.template_name == _DAILY_CARD_TEMPLATE,
            NotificationLog.content == ref,
        ).limit(1)
    )


async def apply_for_cleaning(
    db: AsyncSession,
    *,
    room_id: str,
    order_id: str,
    requester_open_id: str = "",
    ref_date: date | None = None,
) -> CleaningRequest:
    """保洁点【申请打扫】→ 建一条打扫申请（同房同日幂等，重复点返回同一条）。

    只建申请记录；下门锁码/发密码由调用方（飞书回调层，新开会话、best-effort）做，
    避免把锁供应商往返塞进 3s 应答窗。返回的申请含 request_id 供后续按钮回传。
    """
    ref = ref_date or today_cn()

    async def _existing() -> CleaningRequest | None:
        return (
            await db.execute(
                select(CleaningRequest).where(
                    CleaningRequest.room_id == room_id,
                    CleaningRequest.request_date == ref,
                )
            )
        ).scalar_one_or_none()

    existing = await _existing()
    if existing is not None:
        return existing

    req = CleaningRequest(
        request_id="CR-" + uuid4().hex[:18].upper(),
        room_id=room_id,
        order_id=order_id,
        request_date=ref,
        status=CleaningRequestStatus.requested,
        approval_status=CleaningApprovalStatus.pending,
        requester_open_id=requester_open_id or None,
    )
    db.add(req)
    try:
        await db.commit()
    except IntegrityError:
        # 并发/飞书重试同房同日双插 → 唯一约束挡下，回滚取赢家那条（幂等）。
        await db.rollback()
        winner = await _existing()
        if winner is not None:
            return winner
        raise
    await db.refresh(req)
    return req


async def room_belongs_to_checked_in_order(
    db: AsyncSession, *, room_id: str, order_id: str
) -> bool:
    """校验 room 确属该 checked_in 订单——挡伪造回调申请任意房门锁码（评审安全项）。

    与日期口径无关（只认「在住订单 + 房在单内」），故不依赖时钟、测试稳定；
    「今日新入住/今日退房」的排除由每日卡只列合格房 + 无关渠道拿不到 request 保证。
    """
    order = (
        await db.execute(
            select(Order).where(
                Order.order_id == order_id,
                Order.is_deleted == False,  # noqa: E712
                Order.order_status == OrderStatus.checked_in,
            )
        )
    ).scalar_one_or_none()
    if order is None:
        return False
    if order.room_id == room_id:
        return True
    in_or = (
        await db.execute(
            select(OrderRoom.order_room_id).where(
                OrderRoom.order_id == order_id, OrderRoom.room_id == room_id
            ).limit(1)
        )
    ).scalar_one_or_none()
    return in_or is not None


async def mark_cleaning_done(
    db: AsyncSession, *, request_id: str
) -> CleaningRequest | None:
    """保洁点【打扫完了】→ 申请标 cleaned（幂等）。撤码由调用方另做。"""
    req = (
        await db.execute(
            select(CleaningRequest).where(CleaningRequest.request_id == request_id)
        )
    ).scalar_one_or_none()
    if req is None:
        return None
    if req.status != CleaningRequestStatus.cleaned:
        req.status = CleaningRequestStatus.cleaned
        req.cleaned_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(req)
    return req


async def approve_cleaning_request(
    db: AsyncSession,
    *,
    request_id: str,
    approver_open_id: str,
    ref_date: date | None = None,
) -> tuple[CleaningRequest, Expense | None]:
    """前台/管家点【通过】→ 生成保洁费用(30，房东全担) 并标 approved。

    幂等：已 approved 直接返回原申请+已挂费用，不重复扣。
    **并发安全**：先用原子条件 UPDATE(WHERE approval_status != approved) 抢占审批权，
    只有赢家(rowcount==1)建费用；输家(两人同点/飞书重试)拿已挂费用返回，绝不双扣
    （同 cleaning.complete_cleaning_for_task 的原子闸手法）。
    与前台横条续住登记跨路径去重：同订单同房同日已有一条续住打扫费用则复用它，不新建。
    **红线**：绝不碰房态（房里还住着人）。权限校验由调用方用 is_cleaning_approver 把关。
    """
    ref = ref_date or today_cn()
    now = datetime.now(timezone.utc)

    async def _active_expense(expense_id: str | None) -> Expense | None:
        if not expense_id:
            return None
        return (
            await db.execute(
                select(Expense).where(
                    Expense.expense_id == expense_id,
                    Expense.is_deleted == False,  # noqa: E712  作废后不当作有效计费返回
                )
            )
        ).scalar_one_or_none()

    # 原子抢占：把 pending → approved，rowcount==1 才是本次赢家。
    res = await db.execute(
        update(CleaningRequest)
        .where(
            CleaningRequest.request_id == request_id,
            CleaningRequest.approval_status != CleaningApprovalStatus.approved,
        )
        .values(
            approval_status=CleaningApprovalStatus.approved,
            approver_open_id=approver_open_id or None,
            approved_at=now,
        )
    )
    won = (res.rowcount or 0) == 1

    req = (
        await db.execute(
            select(CleaningRequest).where(CleaningRequest.request_id == request_id)
        )
    ).scalar_one_or_none()
    if req is None:
        await db.rollback()
        return None, None  # type: ignore[return-value]

    if not won:
        # 输家 / 已审批过：不重复建费用，返回已挂的有效费用。
        exp = await _active_expense(req.expense_id)
        await db.commit()
        return req, exp

    order = (
        await db.execute(select(Order).where(Order.order_id == req.order_id))
    ).scalar_one_or_none()
    room = (
        await db.execute(select(Room).where(Room.room_id == req.room_id))
    ).scalar_one_or_none()
    if order is None or room is None or not room.owner_id:
        # 无业主/订单丢失 → 无法计费；approved 已由原子 UPDATE 落定，直接提交、expense_id 留空。
        await db.commit()
        await db.refresh(req)
        return req, None

    # 跨路径去重：同订单同房同日已有续住打扫费用则复用（前台横条可能先登记过）。
    exp = (
        await db.execute(
            select(Expense).where(
                Expense.order_id == order.order_id,
                Expense.room_id == room.room_id,
                Expense.category == ExpenseCategory.cleaning,
                Expense.expense_date == ref,
                Expense.is_deleted == False,  # noqa: E712
                Expense.description.like("续住打扫%"),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if exp is None:
        exp = Expense(
            expense_id="EXP-" + uuid4().hex[:12].upper(),
            category=ExpenseCategory.cleaning,
            amount=INSTAY_CLEANING_FEE,
            description=f"续住打扫 {room.room_name}",
            expense_date=ref,
            room_id=room.room_id,
            order_id=order.order_id,
            payer=ExpensePayer.owner,
            owner_id=room.owner_id,
            notes=req.notes,
        )
        db.add(exp)
        await db.flush()

    # approved/approver/approved_at 已由上方原子 UPDATE 落定，这里只回填 expense_id。
    req.expense_id = exp.expense_id
    await db.commit()
    await db.refresh(req)
    await db.refresh(exp)
    return req, exp
