"""续住关联组：软关联同一段连续入住的多张订单（不合并、不删单）。

组内每张单保留自己的渠道/佣金/platform_order_id/actual_price。本模块只负责
「谁和谁是一组」以及「锚单/末段」判定，供门锁延期、押金收口、保洁抑制复用。

只覆盖不换房续住（同 room_id）。换房续住走原有新单流程，不进组。
"""
from __future__ import annotations

import secrets
from datetime import date, timedelta

from sqlalchemy import select

from app.models.order import Order


class SettledStayMutationError(RuntimeError):
    """Ordinary continuation mutation cannot dismantle a managed split."""

    code = "INVALID_SPONSORSHIP_TRANSITION"


def new_stay_group_id() -> str:
    return "sg_" + secrets.token_hex(6)   # sg_ + 12 hex = 15 chars（<= String(32)）


async def get_group_orders(db, stay_group_id: str) -> list[Order]:
    """组内订单，按入住先后排。order_id 兜底保证同日同秒也有确定顺序（同 group_anchor
    的兜底，eng-review #4）：门锁靠「第一条 = 续住段沿用的那把码」选码，顺序不定就会
    出现「入住时延了 A 的码、告警时查 B 的码」的分家。"""
    rows = (await db.execute(
        select(Order)
        .where(Order.stay_group_id == stay_group_id, Order.is_deleted.is_(False))
        .order_by(Order.check_in_date.asc(), Order.created_at.asc(), Order.order_id.asc())
    )).scalars().all()
    return list(rows)


async def managed_group(db, stay_group_id: str | None, *, for_update: bool = False):
    """Return the explicit aggregate; ordinary continuation groups have no row."""
    if not stay_group_id:
        return None
    from app.models.managed_stay_group import ManagedStayGroup

    statement = select(ManagedStayGroup).where(
        ManagedStayGroup.stay_group_id == stay_group_id
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def is_managed_split(db, stay_group_id: str | None) -> bool:
    from app.models.managed_stay_group import ManagedStayGroupKind

    aggregate = await managed_group(db, stay_group_id)
    return aggregate is not None and aggregate.kind is ManagedStayGroupKind.managed_split


async def lock_managed_aggregates_for_members(db, orders: list[Order]):
    """Resolve managed membership, then lock sources before aggregates canonically.

    The initial aggregate lookup is deliberately read-only: it identifies the
    source rows without taking locks.  Every actual lock is then acquired in
    the global source-order-first, aggregate-second order, with sorted IDs for
    multi-order operations such as room swap.
    """
    from app.models.managed_stay_group import ManagedStayGroup, ManagedStayGroupKind

    group_ids = sorted({order.stay_group_id for order in orders if order.stay_group_id})
    if not group_ids:
        return []
    candidates = list((await db.execute(
        select(ManagedStayGroup).where(
            ManagedStayGroup.stay_group_id.in_(group_ids),
            ManagedStayGroup.kind == ManagedStayGroupKind.managed_split,
        )
    )).scalars().all())
    if not candidates:
        return []

    source_ids = sorted({aggregate.source_order_id for aggregate in candidates})
    await db.execute(
        select(Order.order_id)
        .where(Order.order_id.in_(source_ids))
        .order_by(Order.order_id)
        .with_for_update()
    )
    locked = list((await db.execute(
        select(ManagedStayGroup)
        .where(
            ManagedStayGroup.stay_group_id.in_(sorted(
                aggregate.stay_group_id for aggregate in candidates
            )),
            ManagedStayGroup.kind == ManagedStayGroupKind.managed_split,
        )
        .order_by(ManagedStayGroup.stay_group_id)
        .with_for_update()
    )).scalars().all())
    locked_ids = {aggregate.stay_group_id for aggregate in locked}
    return [
        aggregate for aggregate in locked
        if aggregate.stay_group_id in locked_ids
        and any(order.stay_group_id == aggregate.stay_group_id for order in orders)
    ]


async def reject_managed_ordinary_mutation(db, *orders: Order) -> None:
    if await lock_managed_aggregates_for_members(db, list(orders)):
        raise SettledStayMutationError(
            "managed_split 不能通过普通订单入口修改结构、日期、房间或价格；请使用住宿段更正流程"
        )


async def operational_group_orders(db, order: Order) -> list[Order]:
    """Door-lock/checkout/cleaning members for the order's physical room leg.

    Ordinary continuations retain their historical whole-group behavior.  A
    managed split can cross rooms, so only members sharing an ``order_rooms``
    room with the current segment may share codes or suppress checkout/cleaning.
    """
    if not order.stay_group_id:
        return [order]
    members = await get_group_orders(db, order.stay_group_id)
    if not await is_managed_split(db, order.stay_group_id):
        return members

    from app.models.order_room import OrderRoom

    current_rooms = set((await db.execute(
        select(OrderRoom.room_id).where(
            OrderRoom.order_id == order.order_id,
            OrderRoom.room_id.isnot(None),
        )
    )).scalars().all())
    if not current_rooms and order.room_id:
        current_rooms.add(order.room_id)
    member_rooms = (await db.execute(
        select(OrderRoom.order_id, OrderRoom.room_id).where(
            OrderRoom.order_id.in_([member.order_id for member in members]),
            OrderRoom.room_id.isnot(None),
        )
    )).all()
    rooms_by_order: dict[str, set[str]] = {member.order_id: set() for member in members}
    for order_id, room_id in member_rooms:
        rooms_by_order[order_id].add(room_id)
    for member in members:
        if not rooms_by_order[member.order_id] and member.room_id:
            rooms_by_order[member.order_id].add(member.room_id)

    ordered = sorted(members, key=lambda item: (item.check_in_date, item.order_id))
    current_index = next(
        (index for index, member in enumerate(ordered) if member.order_id == order.order_id),
        None,
    )
    if current_index is None:
        return [order]

    start = end = current_index
    while start > 0:
        previous, current = ordered[start - 1], ordered[start]
        if (
            previous.check_out_date != current.check_in_date
            or not (rooms_by_order[previous.order_id] & rooms_by_order[current.order_id])
        ):
            break
        start -= 1
    while end + 1 < len(ordered):
        current, following = ordered[end], ordered[end + 1]
        if (
            current.check_out_date != following.check_in_date
            or not (rooms_by_order[current.order_id] & rooms_by_order[following.order_id])
        ):
            break
        end += 1
    return ordered[start:end + 1]


async def operational_group_anchor(db, order: Order, *, alive_only: bool = False) -> Order | None:
    members = await operational_group_orders(db, order)
    if alive_only:
        from app.models.order import OrderStatus
        members = [member for member in members if member.order_status != OrderStatus.cancelled]
    return min(members, key=lambda item: (item.check_in_date, item.order_id), default=None)


async def operational_group_final_checkout_date(
    db, order: Order, *, alive_only: bool = False
) -> date | None:
    members = await operational_group_orders(db, order)
    if alive_only:
        from app.models.order import OrderStatus
        members = [member for member in members if member.order_status != OrderStatus.cancelled]
    return max((member.check_out_date for member in members), default=None)


async def _group_members(db, stay_group_id: str, alive_only: bool) -> list[Order]:
    """组成员。alive_only=True 时排除已取消段。

    默认 False = 三个 caliber 函数直接调用方的行为不变（test_alive_only_does_not_
    change_default_callers 钉死）。展示口径（group_view）与末段判定
    （is_group_last_segment）传 True：取消段若退房日最晚，会把整段日期撑长、
    还会让 last_order_id 指向它，真末段被 400 拦住，前台就在 UI 上退不了房。
    """
    from app.models.order import OrderStatus

    orders = await get_group_orders(db, stay_group_id)
    if alive_only:
        orders = [o for o in orders if o.order_status != OrderStatus.cancelled]
    return orders


async def group_anchor(db, stay_group_id: str, alive_only: bool = False) -> Order | None:
    """组内最早入住那张 = 持码锚单（同日入住用 order_id 兜底，保证唯一）。"""
    orders = await _group_members(db, stay_group_id, alive_only)
    if not orders:
        return None
    return min(orders, key=lambda o: (o.check_in_date, o.order_id))


async def group_final_checkout_date(db, stay_group_id: str,
                                    alive_only: bool = False) -> date | None:
    orders = await _group_members(db, stay_group_id, alive_only)
    return max((o.check_out_date for o in orders), default=None)


async def group_last_order_id(db, stay_group_id: str, alive_only: bool = False) -> str | None:
    """组内「末段」有且只有一张：退房日最晚，同日用 order_id 兜底，保证唯一。
    （eng-review #4：用 >= 会让同日退房两张单都判为末段 → 押金退两次/保洁收两次。）"""
    orders = await _group_members(db, stay_group_id, alive_only)
    if not orders:
        return None
    return max(orders, key=lambda o: (o.check_out_date, o.order_id)).order_id


async def is_group_last_segment(db, order: Order) -> bool:
    """本单是不是组末段（活段口径，与 group_view 展示一致）。

    alive_only=True：bypms 退订了组末段、自愈还没把取消段摘走时，真末段不能
    被取消段顶着当「中间段」拦住——那会让客人在 UI 上退不了房。
    整组都取消时 last 为 None，此时不拦（返回 True 交给状态机守卫）。
    """
    from app.models.order import OrderStatus

    if not order.stay_group_id:
        return True
    if await is_managed_split(db, order.stay_group_id):
        members = await operational_group_orders(db, order)
        alive = [member for member in members if member.order_status != OrderStatus.cancelled]
        if not alive:
            return True
        return order.order_id == max(
            alive, key=lambda item: (item.check_out_date, item.order_id)
        ).order_id
    last = await group_last_order_id(db, order.stay_group_id, alive_only=True)
    return last is None or order.order_id == last


async def is_group_first_segment(db, order: Order) -> bool:
    """本单是不是组首段（活段口径，与 is_group_last_segment 对称）。

    一趟续住只在**首段入住日**要办一次入住：后续段的入住日到来时客人已经在店，
    前台不需要为它办任何手续。「今日待入住」看板据此剔除非首段，否则续住第二段会
    以「待入住」示人——生产 2026-07-25 sg_24b6b1aa12ba（1615 房）就是这么被前台
    投诉「明明已经入住了还显示待入住」的。

    alive_only=True：取消段若入住日最早会把锚单顶掉，真首段反被判成「续段」而
    从待入住里消失，前台就看不到今天该来的客人了。整组皆取消时 anchor 为 None，
    此时不拦（返回 True）。
    """
    from app.models.order import OrderStatus

    if not order.stay_group_id:
        return True
    if await is_managed_split(db, order.stay_group_id):
        members = await operational_group_orders(db, order)
        alive = [member for member in members if member.order_status != OrderStatus.cancelled]
        if not alive:
            return True
        return order.order_id == min(
            alive, key=lambda item: (item.check_in_date, item.order_id)
        ).order_id
    anchor = await group_anchor(db, order.stay_group_id, alive_only=True)
    return anchor is None or order.order_id == anchor.order_id


async def complete_group_siblings(db, order: Order) -> None:
    """末段完成 → 级联把同组其它非终态段也标完成（中间段退房被拦，只能靠这里收
    尾；否则它们永远卡 checked_in，房态板假在住、收入确认不触发）。系统级联，
    直接置状态。单笔与批量流转共用，非末段/非组单为 no-op。"""
    from app.models.order import OrderStatus

    if not order.stay_group_id:
        return
    if not await is_group_last_segment(db, order):
        return
    siblings = (
        await operational_group_orders(db, order)
        if await is_managed_split(db, order.stay_group_id)
        else await get_group_orders(db, order.stay_group_id)
    )
    for sib in siblings:
        if sib.order_id != order.order_id and sib.order_status not in (
            OrderStatus.completed, OrderStatus.cancelled,
        ):
            sib.order_status = OrderStatus.completed
    await db.flush()


async def deposit_holder(db, order: Order) -> Order:
    """续住组里「押金实际挂在哪张单」——押金一趟只收一次，收退都认这一张。非组单返回自己。

    口径：组内活段按入住日排序，优先取第一个 **collected**（此刻真押着钱的那段）；
    没有则取第一个非 not_collected 的（押金已结算过的组，供展示「已退/已扣」）；
    全未收才回退锚单（首段，同样只认活段）。

    **为什么 collected 要优先，而不是只判「非 not_collected」**（2026-07-17 /review 抓到）：
    link_continuation 的 _base_terminal 旁路允许把 deposit_status=returned 的终态老单
    当锚单关联续住——补录历史单正是这么用的。只判「非 not_collected」时 returned 也满足，
    已退款的老单就会遮蔽真正押着钱的续段，退房结算判 == collected 为假 → 静默跳过。
    那正是本函数当初要修的 bug 从另一个角度长出来。

    **为什么不是「只认锚单」**（2026-07-17 修）：事后补关联历史老单会产出「押金收在
    续段、锚单 ¥0 未收」的组——生产实例 sg_811e2711543e 就是。旧口径返回锚单，退房
    结算分支（staff_portal.py 的 `deposit_status == collected` 判定）为假 → **静默
    跳过：不退款、不报错**。押金收退在系统里是手点标记（真钱走 POS），所以这不是系统
    吞钱，而是页面显示「¥0 未收」→ 前台不被提醒 → 漏退现金、账也不落库。前台 7/14
    一口气补关联了 21 组，这种形态只会变多。

    回退锚单这条保证了新组行为不变：全未收时收押金照旧挂锚单。

    **必须与 group_view 的 dep_seg 同规则**——展示口径说「已收 ¥100」而操作口径找不到
    那张单，等于页面骗人、钱不退。test_deposit_holder_matches_group_view 钉死了这点。
    """
    from app.models.order import DepositStatus

    if not order.stay_group_id:
        return order

    # alive_only=True：回退锚单也必须是活着的那段，否则同一份响应里
    # anchor_order_id（group_view 用 alive_only）与 deposit_order_id 会指向两张不同的单，
    # 页面让前台去一张已取消的单上收押金。
    aggregate = await managed_group(db, order.stay_group_id)
    from app.models.managed_stay_group import ManagedStayGroupKind

    if aggregate is not None and aggregate.kind is ManagedStayGroupKind.managed_split:
        # A managed split may cross rooms, but the deposit remains one financial
        # fact for the whole stay.  Deliberately use the aggregate source rather
        # than the physical-leg helper used by locks/checkout/cleaning.
        anchor = await db.get(Order, aggregate.source_order_id) or order
    else:
        anchor = await group_anchor(db, order.stay_group_id, alive_only=True) or order
    alive = await _group_members(db, order.stay_group_id, alive_only=True)
    ordered = sorted(alive, key=lambda x: (x.check_in_date, x.order_id))

    # 全未收时优先回退「deposit>0 的最早活段」：押金金额录在续段的组（补录/改单
    # 形态），收押金入口要指到那段，否则前台在 ¥0 锚单上收、¥300 那段永远显示未收。
    # 各段都为 0 才回退锚单——新组（金额录锚单或没录）收押金行为一个字节不变。
    from decimal import Decimal

    return next(
        (s for s in ordered if s.deposit_status == DepositStatus.collected),
        next(
            (s for s in ordered if s.deposit_status != DepositStatus.not_collected),
            next((s for s in ordered if (s.deposit or Decimal("0")) > 0), anchor),
        ),
    )


async def link_continuation(db, base: Order, nxt: Order) -> str:
    """把 nxt 关联为 base 的续住续单，两张共享一个 stay_group_id（base 已有组则继承）。

    校验：nxt 必须是 base 的合法续住续单（同房+日期相连+未退房），复用
    find_group_continuations 的匹配口径。**不要求同名**——夫妻俩用两个名字各定一张续住
    单也要能手动关联（require_same_name=False）；同名限制只属于自动提醒那层。
    校验失败抛 ValueError。

    base 是**前台点开的那张单**，组的解析在 find_group_continuations 里按房间做（#339 那版
    先把 base 换成组末段单，一单多房时会丢掉另一间房那条腿，见该函数注释）。
    """
    from app.models.order import DepositStatus, OrderStatus
    from app.services.checkout_continuation import find_group_continuations, _LINKABLE_STATUSES

    if (
        await is_managed_split(db, base.stay_group_id)
        or await is_managed_split(db, nxt.stay_group_id)
    ):
        raise SettledStayMutationError(
            "managed_split 不能通过普通续住入口增减成员；请使用住宿段更正流程"
        )

    # 候选放宽到「已退房/已完成」老单也算——支持关联历史续住老单（为后期退房费用不重复收）。
    # require_same_name=False：手动关联是人肉确认的动作，允许不同名（如夫妻各定一单）。
    conts = await find_group_continuations(
        db, base, statuses=_LINKABLE_STATUSES, require_same_name=False)
    if not any(c["next_order_id"] == nxt.order_id for c in conts):
        raise ValueError("不是合法的续住续单：需同房间、退房日紧接入住日、且续住单未取消")
    # eng-review #2：首段押金已退/已扣，就不能再作为续住组的押金持有段。
    # 但历史老单(已退房/已完成)的押金本就结算过，关联只是打「一趟」记号供做账不重复收费，
    # 不再涉及押金持有 → 对终态老单跳过此闸。
    _base_terminal = base.order_status in (OrderStatus.completed, OrderStatus.pending_checkout)
    if not _base_terminal and base.deposit_status in (DepositStatus.returned, DepositStatus.withheld):
        raise ValueError("首段押金已退还/扣除，无法关联续住；请先撤销退款，或改在续住段单独收押金后再关联")
    gid = base.stay_group_id or nxt.stay_group_id or new_stay_group_id()
    base.stay_group_id = gid
    nxt.stay_group_id = gid
    await db.flush()
    return gid


async def unlink_order(db, order: Order) -> None:
    """把该单移出续住组。若组内因此只剩一张，另一张也一并清空（组失去意义）。

    摘除使组末退房日提前时，best-effort 把剩余段的共享客人码有效期缩到新组末
    （共享码覆盖整段，组变短码不缩 = 客人走后码仍能开门）。门锁侧任何失败都
    不抛——unlink 还被取消/删除流程复用，绝不能因门锁把主流程炸掉。
    """
    gid = order.stay_group_id
    if await is_managed_split(db, gid):
        raise SettledStayMutationError(
            "managed_split 不能通过普通取消关联或删除入口拆解；请使用住宿段更正流程"
        )
    old_final = await group_final_checkout_date(db, gid, alive_only=True) if gid else None
    order.stay_group_id = None
    await db.flush()
    if not gid:
        return
    remaining = await get_group_orders(db, gid)
    if len(remaining) == 1:
        remaining[0].stay_group_id = None
        await db.flush()
        return  # 组已解散：剩下那张回归普通单，码的有效期归它自己的流程管
    new_final = await group_final_checkout_date(db, gid, alive_only=True)
    if not remaining or new_final is None or new_final == old_final:
        return
    # best-effort 收码：只 flush 不 commit（事务归调用方），失败按续住关联
    # 延期失败的同款口径告警到密码群，绝不抛错。
    import logging
    try:
        from app.services.lock import factory as _lock_factory
        from app.services.lock.orchestrator import OrderLockService as _LockSvc
        results = await _LockSvc(_lock_factory.get_lock_provider(), db).renew_group_codes(
            gid, new_final, commit=False)
        if results and any(not r.get("ok") for r in results):
            from app.services.feishu_lead_alert import send_password_text
            await send_password_text(
                f"⚠️ 续住组变更后门锁有效期调整未坐实 组{gid}→{new_final}，"
                f"请到慧享家 App 手动把该房密码调整到 {new_final} 14:00")
    except Exception:
        logging.getLogger(__name__).exception("unlink 后调整门锁有效期失败 group=%s", gid)


async def selfheal_stay_groups(db) -> int:
    """定时自愈兜底（eng-review #3）：把续住组里已取消/已删除的成员解关联。

    bypms 同步引擎在独立仓、直接改库退订我们的单，不会调 app 侧 unlink。此扫描是
    catch-all：不管成员因何变终态，都把它从组里摘掉，避免组挂着一张退订孤儿单误导
    门锁/看板/报表。返回解关联的成员数。gate 由调用方（celery 任务）用环境变量控制。
    """
    from app.models.order import OrderStatus

    # 找出仍带组号、但已取消/已删除的成员
    dead = (await db.execute(
        select(Order).where(
            Order.stay_group_id.isnot(None),
            (Order.is_deleted.is_(True)) | (Order.order_status == OrderStatus.cancelled),
        )
    )).scalars().all()
    healed = 0
    for o in dead:
        if await is_managed_split(db, o.stay_group_id):
            continue  # managed membership is corrected only through its versioned aggregate flow
        # 直接清本单组号（不复用 unlink_order 的"剩一张也清空"逻辑，避免对已删单做多余处理）
        gid = o.stay_group_id
        o.stay_group_id = None
        healed += 1
        await db.flush()
        if gid:
            remaining = await get_group_orders(db, gid)  # get_group_orders 已排除 is_deleted
            alive = [r for r in remaining if r.order_status != OrderStatus.cancelled]
            if len(alive) == 1:
                alive[0].stay_group_id = None
                await db.flush()
    return healed


async def group_view(db, order: Order) -> dict:
    """整段视图口径：把一个续住组聚合成「一段连续入住」的展示数据。

    锚单/末段/最晚退房日全部复用本模块既有函数，不另立口径。非组单返回只有
    一段的退化组，让调用方一套逻辑走通。

    三条本函数确立的展示口径：

    1. 整段状态：任一段待退房→待退房；否则任一段在住→在住；否则取锚单状态。
       （客人没到店时锚单状态即整段状态；客人走后末段完成会级联全组 completed。）

    2. 押金：调 deposit_holder()，与收退押金的操作口径同一个函数。展示说「已收 ¥100」
       而操作找不到那张单，等于页面骗人、钱不退（事后补关联历史单会产出「押金收在续段」
       的组，生产实例 sg_811e2711543e）。

    3. 房间：从 order_rooms 按日期取去重房号，**不读 orders.room_id**（该字段
       在 schemas/order.py 标了 DEPRECATED，多房单上是不可靠的首房快照）。
       跨房间续住组（生产实例 sg_4909d1907fb8）会返回 ["1605", "1606"]。

    已取消段照常列在 segment_order_ids 里供前端打标，但**不计入 total_amount、
    也不参与日期与末段判定**（靠三个 caliber 函数的 alive_only=True）。否则一个
    付费夜会渲染成「2 晚 ¥300」，而 last_order_id 指向取消段还会让前台在 UI 上
    退不了房。摘除取消成员本身是 selfheal_stay_groups 的活，此处不重复实现。
    """
    from decimal import Decimal

    from sqlalchemy import select as _select

    from app.models.order import DepositStatus, OrderStatus
    from app.models.order_room import OrderRoom

    gid = order.stay_group_id
    aggregate = await managed_group(db, gid)
    if not gid:
        segments = [order]
        anchor = order
        last_id = order.order_id
        final_co = order.check_out_date
    else:
        segments = await get_group_orders(db, gid)
        # alive_only=True：已取消段不参与日期与末段判定。
        # 若整组都取消了，三个函数都返回 None，回退到 order 自身 —— 页面还能开。
        anchor = await group_anchor(db, gid, alive_only=True) or order
        last_id = await group_last_order_id(db, gid, alive_only=True) or order.order_id
        final_co = await group_final_checkout_date(db, gid, alive_only=True) or order.check_out_date

    seg_ids = [s.order_id for s in segments]
    alive = [s for s in segments if s.order_status != OrderStatus.cancelled]

    from app.core.free_room import free_room_kind
    free_kind = free_room_kind(alive if gid else segments)

    # 整段状态（口径 1）
    if any(s.order_status == OrderStatus.pending_checkout for s in alive):
        status = OrderStatus.pending_checkout
    elif any(s.order_status == OrderStatus.checked_in for s in alive):
        status = OrderStatus.checked_in
    else:
        status = anchor.order_status

    total = sum((s.actual_price or Decimal("0")) for s in alive) or Decimal("0")

    # 渠道只取活段：取消的携程段会把整段误标成「携程」，误导对账归因。
    channels: list[str] = []
    for s in alive:
        c = s.channel.value if hasattr(s.channel, "value") else s.channel
        if c not in channels:
            channels.append(c)

    # 押金：与收退押金的操作口径同一个函数，不留两份（口径 2）
    dep_seg = await deposit_holder(db, order)

    # 房间序列（口径 3）＋逐段房号（segment_details 用，同样不读 orders.room_id）
    room_rows = (await db.execute(
        _select(OrderRoom.order_id, OrderRoom.room_id)
        .where(OrderRoom.order_id.in_(seg_ids), OrderRoom.room_id.isnot(None))
        .order_by(OrderRoom.check_in_date.asc(), OrderRoom.position.asc())
    )).all()
    rooms: list[str] = []
    rooms_by_seg: dict[str, list[str]] = {sid: [] for sid in seg_ids}
    for oid, r in room_rows:
        if r not in rooms:
            rooms.append(r)
        if r not in rooms_by_seg[oid]:
            rooms_by_seg[oid].append(r)

    return {
        "stay_group_id": gid,
        "group_kind": aggregate.kind.value if aggregate is not None else None,
        "anchor_order_id": anchor.order_id,
        "last_order_id": last_id,
        "check_in_date": anchor.check_in_date,
        "check_out_date": final_co,
        # 晚数 = 活段覆盖的**去重日历夜**（区间并集），不用「首段入住→末段退房」的
        # 跨度，也不逐段求和：跨度会把取消的中间段那一夜算进来而金额没算（「3 晚
        # ¥300」）；求和在多房组里会把时间并行的段重复计数（程鹏 1608+1609：
        # 1+2+2=5 晚，实住 3 个日历夜）。并集两个坑都避开。
        "nights": len({
            s.check_in_date + timedelta(days=i)
            for s in alive
            for i in range((s.check_out_date - s.check_in_date).days)
        }) or (final_co - anchor.check_in_date).days,
        "total_amount": total,
        "group_status": status,
        "channels": channels,
        "rooms": rooms,
        "free_room_kind": free_kind,
        "deposit": dep_seg.deposit or Decimal("0"),
        "deposit_status": dep_seg.deposit_status,
        "deposit_order_id": dep_seg.order_id,
        "deposit_returned": dep_seg.deposit_returned,
        "segment_order_ids": seg_ids,
        # 逐段明细：房号列表 + 每段押金（前端段列表直用，不必再拆 OrderOut）
        "segment_details": [
            {
                "order_id": s.order_id,
                "rooms": rooms_by_seg.get(s.order_id, []),
                "deposit": s.deposit or Decimal("0"),
                "deposit_status": s.deposit_status,
            }
            for s in segments
        ],
    }
