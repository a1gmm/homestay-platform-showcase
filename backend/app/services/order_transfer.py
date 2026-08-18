"""房间转移（换房）核心逻辑。

改 ORM 对象（不 commit，调用方负责），调用方已校验状态/冲突/目标房存在。

ASCII — 入住中换房按换房日拆 OrderRoom：
    原行 [入住 ─────────────── 退房)   room=A
            │ transfer_date
            ▼
    旧行 [入住 ── td)  room=A   +   新行 [td ── 退房)  room=B(+加价)
"""
from datetime import date
from decimal import Decimal

from app.core.ids import gen_order_room_id
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room, RoomStatus
from app.schemas.order import TransferReason, OldRoomDisposition
from app.services.order_pricing import (
    compute_daily_prices, sum_daily_prices, split_daily_prices_by_date,
    clear_per_room_owner_revenue,
)


def _default_disposition(reason: TransferReason, midstay: bool) -> OldRoomDisposition:
    if reason == TransferReason.room_defect:
        return OldRoomDisposition.maintenance
    return OldRoomDisposition.pending_clean if midstay else OldRoomDisposition.available


async def _occupy_new_room(db, room_id: str, status: RoomStatus) -> None:
    """新房置 occupied(入住中)/reserved(入住前)。仅从 available/reserved 提升，
    不踩 pending_clean/cleaning/occupied 等真实态（避免把没打扫的房强标在住）。"""
    room = await db.get(Room, room_id)
    if room and room.room_status in (RoomStatus.available, RoomStatus.reserved):
        room.previous_status = room.room_status
        room.room_status = status


async def _dispose_old_room(db, room_id: str, disposition: OldRoomDisposition) -> None:
    """旧房置 maintenance/pending_clean/available。previous_status 一律记 available
    （客人已搬走，旧房本质是空的；维修「一键恢复」回 available，不会错标在住）。"""
    room = await db.get(Room, room_id)
    if not room:
        return
    target = RoomStatus(disposition.value)
    if room.room_status != target:
        room.previous_status = RoomStatus.available
        room.room_status = target


async def apply_room_transfer(
    db, order: Order, *, order_room_id: str, new_room_id: str,
    reason: TransferReason, transfer_date: date,
    markup_amount: Decimal, old_room_disposition: OldRoomDisposition | None,
) -> OrderRoom:
    """换房核心：改 ORM 对象（不 commit）。返回客人最终所在的新房行。
    transfer_date 必传：默认值（midstay=本地今天 / 入住前=入住日）由端点用 _china_today()
    算好后传入，避免服务层再用 UTC date.today() 引入跨零点 bug（#97）。"""
    target = next((r for r in order.rooms if r.order_room_id == order_room_id), None)
    if target is None:
        raise ValueError(f"order_room_id {order_room_id} 不属于本订单")
    old_room_id = target.room_id

    # 每房净房费(B 方案)与换房互斥：换房拆分/加价后各房佣金归属说不清，手填的
    # 每房净值一律清空（含兄弟行）回退比例口径，留痕供人工事后补录（共享清值 helper）。
    clear_per_room_owner_revenue(order, reason="room_transfer")

    midstay = order.order_status == OrderStatus.checked_in
    eff_date = transfer_date
    disposition = old_room_disposition or _default_disposition(reason, midstay)

    do_split = midstay and eff_date > target.check_in_date

    if do_split:
        daily = dict(target.daily_prices or {}) or compute_daily_prices(
            target.check_in_date, target.check_out_date, target.actual_price)
        before, after = split_daily_prices_by_date(daily, eff_date)
        old_checkout = target.check_out_date
        # 旧房行收缩到 [入住, 换房日)
        target.check_out_date = eff_date
        target.daily_prices = before
        target.actual_price = sum_daily_prices(before)
        target.metadata_ = {**(target.metadata_ or {}),
            "transfer": {"reason": reason.value, "to_room": new_room_id, "date": eff_date.isoformat()}}
        # 新房行 [换房日, 退房)：沿用原单晚价（不抹平），加价并入首晚。
        # new_actual 始终 = sum(after) + 加价；待定价订单 after 为空时不会崩（不折进日价）。
        new_daily = dict(after)
        new_actual = sum_daily_prices(new_daily) + markup_amount
        if markup_amount and new_daily:
            first_key = min(new_daily)
            new_daily[first_key] = str(Decimal(new_daily[first_key]) + markup_amount)
        new_pos = max((r.position for r in order.rooms), default=-1) + 1
        new_row = OrderRoom(
            order_room_id=gen_order_room_id(), order_id=order.order_id, room_id=new_room_id,
            check_in_date=eff_date, check_out_date=old_checkout,
            actual_price=new_actual, daily_prices=new_daily,
            guests_count=target.guests_count, position=new_pos,
            metadata_={"transfer": {"reason": reason.value, "from_room": old_room_id,
                                    "date": eff_date.isoformat(), "sibling": target.order_room_id}},
        )
        order.rooms.append(new_row)
        final_row = new_row
    else:
        # 整段移动：只改 room_id + 加价。加价折进首晚，保留原单晚价（不 re-average 抹平周末/平日差异）。
        target.room_id = new_room_id
        if markup_amount:
            daily = dict(target.daily_prices or {}) or compute_daily_prices(
                target.check_in_date, target.check_out_date, target.actual_price)
            if daily:
                fk = min(daily)
                daily[fk] = str(Decimal(daily[fk]) + markup_amount)
                target.daily_prices = daily
                target.actual_price = sum_daily_prices(daily)
            else:
                target.actual_price = (target.actual_price or Decimal("0")) + markup_amount
        target.metadata_ = {**(target.metadata_ or {}),
            "transfer": {"reason": reason.value, "from_room": old_room_id, "to_room": new_room_id}}
        final_row = target

    # 订单总价重算（payment_status 重算放端点层）
    order.actual_price = sum((r.actual_price or Decimal("0")) for r in order.rooms)

    # 同步 deprecated 顶层 order.room_id → 客人当前所在房（换房后的目标房 final_row）。
    # 不能用 position 最小行：入住中拆分后那是已腾退的旧房，会让通知/门锁(Phase2)指向错房。
    if final_row.room_id:
        order.room_id = final_row.room_id

    # 手动换房锁（镜像 price_locked）：在我们系统里换过房后，房号以运营为准。打此标记让
    # ota-sync 的 bypms 房号对账（apply_room_reconciliation）跳过本单、不把房号倒回 bypms 原房
    # ——否则下一轮同步会覆盖，导致已发的门锁码与 DB 房号不符（进错房事故，landmine）。
    order.metadata_ = {**(order.metadata_ or {}), "room_locked": True}

    # 房态双真相 — 静态枚举（按日期真相由 OrderRoom 行保证）
    await _occupy_new_room(db, new_room_id, RoomStatus.occupied if midstay else RoomStatus.reserved)
    if old_room_id and old_room_id != new_room_id:
        await _dispose_old_room(db, old_room_id, disposition)

    return final_row
