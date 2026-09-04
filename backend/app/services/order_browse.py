"""订单管理页的整段日期口径。

订单页一行代表一个续住组（无组单退化成单成员组），因此日期筛选必须使用这一行
展示的首次入住日/最终退房日。原始 ``GET /orders`` 仍按单过滤，供 Dashboard 事件
清单使用；两类端点不要共用日期表达式。
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom


class OrderDateBasis(str, Enum):
    final_checkout = "final_checkout"
    first_checkin = "first_checkin"


@dataclass(frozen=True)
class OrderBrowseDateFilter:
    basis: OrderDateBasis
    date_from: Optional[date]
    date_to: Optional[date]


def resolve_order_browse_date_filter(
    *,
    date_basis: Optional[OrderDateBasis],
    date_from: Optional[date],
    date_to: Optional[date],
    check_in_from: Optional[date],
    check_in_to: Optional[date],
    check_out_from: Optional[date],
    check_out_to: Optional[date],
) -> Optional[OrderBrowseDateFilter]:
    """校验新契约，并把历史深链参数收敛成同一内部模型。

    新契约必须完整传 ``date_basis/date_from/date_to``；历史链接仍可只传入住或退房
    上下界。新旧参数混用以及同时传两种历史日期口径都会直接拒绝，避免静默猜测。
    """
    canonical = (date_basis, date_from, date_to)
    canonical_present = any(value is not None for value in canonical)
    legacy_checkin_present = check_in_from is not None or check_in_to is not None
    legacy_checkout_present = check_out_from is not None or check_out_to is not None
    legacy_present = legacy_checkin_present or legacy_checkout_present

    if canonical_present and legacy_present:
        raise HTTPException(status_code=422, detail="不能混用新旧日期筛选参数")

    if canonical_present:
        if not all(value is not None for value in canonical):
            raise HTTPException(
                status_code=422,
                detail="date_basis、date_from、date_to 必须同时提供",
            )
        assert date_basis is not None and date_from is not None and date_to is not None
        if date_to < date_from:
            raise HTTPException(status_code=422, detail="date_to 不能早于 date_from")
        return OrderBrowseDateFilter(date_basis, date_from, date_to)

    if legacy_checkin_present and legacy_checkout_present:
        raise HTTPException(status_code=422, detail="入住日和退房日筛选不能同时提供")
    if legacy_checkin_present:
        if check_in_from is None or check_in_to is None:
            raise HTTPException(status_code=422, detail="check_in_from 和 check_in_to 必须同时提供")
        if check_in_to < check_in_from:
            raise HTTPException(status_code=422, detail="check_in_to 不能早于 check_in_from")
        return OrderBrowseDateFilter(OrderDateBasis.first_checkin, check_in_from, check_in_to)
    if legacy_checkout_present:
        if check_out_from is None or check_out_to is None:
            raise HTTPException(status_code=422, detail="check_out_from 和 check_out_to 必须同时提供")
        if check_out_to < check_out_from:
            raise HTTPException(status_code=422, detail="check_out_to 不能早于 check_out_from")
        return OrderBrowseDateFilter(OrderDateBasis.final_checkout, check_out_from, check_out_to)
    return None


def stay_group_dates_subquery():
    """返回与 ``stay_group.group_view`` 相同的整段日期聚合。

    正常情况下取消成员不参与首/末日期；整组都取消时回退到全部成员，保证已取消筛选
    仍能定位并展示这组历史记录。
    """
    group_key = func.coalesce(Order.stay_group_id, Order.order_id)
    active_checkin = case(
        (Order.order_status != OrderStatus.cancelled, Order.check_in_date),
        else_=None,
    )
    active_checkout = case(
        (Order.order_status != OrderStatus.cancelled, Order.check_out_date),
        else_=None,
    )
    return (
        select(
            group_key.label("group_key"),
            func.coalesce(func.min(active_checkin), func.min(Order.check_in_date)).label(
                "first_checkin"
            ),
            func.coalesce(func.max(active_checkout), func.max(Order.check_out_date)).label(
                "final_checkout"
            ),
        )
        .where(Order.is_deleted == False)
        .group_by(group_key)
        .subquery("stay_group_dates")
    )


def group_date_conditions(group_dates, date_filter: Optional[OrderBrowseDateFilter]) -> list:
    if date_filter is None:
        return []
    column = (
        group_dates.c.final_checkout
        if date_filter.basis == OrderDateBasis.final_checkout
        else group_dates.c.first_checkin
    )
    conditions = []
    if date_filter.date_from is not None:
        conditions.append(column >= date_filter.date_from)
    if date_filter.date_to is not None:
        conditions.append(column <= date_filter.date_to)
    return conditions


def matching_group_keys_statement(
    *,
    member_conditions: list,
    date_filter: Optional[OrderBrowseDateFilter],
):
    """列表总数、分页与导出共用的命中组集合，并提供稳定排序。"""
    group_key = func.coalesce(Order.stay_group_id, Order.order_id)
    group_dates = stay_group_dates_subquery()
    return (
        select(group_key.label("group_key"))
        .join(group_dates, group_dates.c.group_key == group_key)
        .where(*member_conditions, *group_date_conditions(group_dates, date_filter))
        .group_by(group_key)
        .order_by(func.max(Order.created_at).desc(), group_key.asc())
    )


def order_browse_member_conditions(
    *,
    status: Optional[str] = None,
    channel=None,
    room_id: Optional[str] = None,
    keyword: Optional[str] = None,
) -> list:
    """整段列表/导出的非日期条件：成员命中则整段入选。"""
    conditions = [Order.is_deleted == False]
    if status:
        try:
            statuses = [OrderStatus(item.strip()) for item in status.split(",") if item.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail=f"无效的订单状态: {status}")
        if not statuses:
            raise HTTPException(status_code=422, detail="status 参数为空")
        conditions.append(Order.order_status.in_(statuses))
    else:
        conditions.append(Order.order_status != OrderStatus.cancelled)
    if channel:
        conditions.append(Order.channel == channel)
    if room_id:
        conditions.append(Order.rooms.any(OrderRoom.room_id == room_id))
    if keyword:
        conditions.append(
            or_(
                Order.guest_name.ilike(f"%{keyword}%"),
                Order.guest_phone.ilike(f"%{keyword}%"),
                Order.order_id.ilike(f"%{keyword}%"),
            )
        )
    return conditions
