"""慧享家房态(roomState)对账决策 —— 纯逻辑，无 IO。

慧享家房态图颜色 = 其 roomState 字段（2026-07-14 生产实测坐实）：
    1=闲置(灰)  2=出租/在住(绿)  4=到期(橙)  5=打扫  6=维修
可控手段：下密码(apartmentAddPasswordKey)→2；退房(apartmentRoomCheckOut)→1；
delKey 不改 roomState。故只做「有人(2)/没人(1)」两色对账（业务方拍板）。

本模块只做决策，真正调厂商在 OrderLockService.reconcile_room_states。
"""
from __future__ import annotations

from enum import Enum

# 慧享家 roomState 取值
STATE_VACANT = 1      # 闲置（灰，我们的"没人"）
STATE_OCCUPIED = 2    # 出租/在住（绿，我们的"有人"）


class ReconcileAction(str, Enum):
    checkout = "checkout"        # 没人却显示非闲置 → 退房清成闲置(1)
    alert_stuck = "alert_stuck"  # 有人却显示非在住 → 飞书告警(一期不自动拉回，见 spec §七)
    noop = "noop"                # 已一致 / 拉取失败 / 安全守卫命中，不动


def desired_room_state(occupied: bool) -> int:
    """我们系统真实占用 → 期望的慧享家 roomState。"""
    return STATE_OCCUPIED if occupied else STATE_VACANT


def decide_action(
    occupied: bool,
    current_state: int | None,
    has_active_code: bool,
) -> ReconcileAction:
    """对账决策（纯函数）。

    occupied        我们系统按订单/日期判定该房今天是否有人住
    current_state   慧享家当前 roomState；None = 本轮没拉到该房（不确定）→ 不动
    has_active_code 该房是否仍有 active/pending 门锁码（安全守卫）

    安全底线：checkOut 会清空整房所有授权（含总码/保洁码/管家码）。故「没人 →
    checkOut」前若房里还有任何在用码，一律 noop——绝不把正在打扫的保洁员 / 系统误判
    下仍有效的客人锁在门外。这条同时天然实现「正在打扫的房先不动」。
    """
    if current_state is None:
        return ReconcileAction.noop

    if occupied:
        # 有人：显示在住就好；否则告警（一期不自动拉回）
        return ReconcileAction.noop if current_state == STATE_OCCUPIED else ReconcileAction.alert_stuck

    # 没人
    if current_state == STATE_VACANT:
        return ReconcileAction.noop
    # 没人却显示非闲置 → 想清成闲置，但先过安全守卫
    return ReconcileAction.noop if has_active_code else ReconcileAction.checkout
