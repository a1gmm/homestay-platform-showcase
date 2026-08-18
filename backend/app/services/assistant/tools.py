"""只读工具白名单（护栏①②）—— 硬编码 dict，每个 = pydantic 参数 + 只读 handler。

安全内核：LLM 只吐 {tool, params}。tool 不在白名单 → UnknownToolError（结构性免疫，不派发）；
params 不合规 → ToolParamError（不执行）。全部 handler 只读，无任何写/SQL/自由执行路径。
查数工具一律复用已对齐口径的现成函数，绝不自算，避免与页面数字分叉。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date as _date
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.services.assistant.periods import PeriodError, resolve_period


class UnknownToolError(Exception):
    """LLM 吐了白名单外的 tool —— 硬拒，绝不派发。"""


class ToolParamError(Exception):
    """参数不合规（pydantic 校验失败 / 缺定位条件）—— 不执行。"""


# ─── 参数模型（extra 默认 ignore：模型多吐的字段被丢弃，不进 handler）──────────────

class PeriodParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    period: str


class RoomRevenueParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    period: str
    room_id: Optional[str] = None


class CleaningWorkloadParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    period: str
    cleaner: Optional[str] = None


class NoParams(BaseModel):
    model_config = ConfigDict(extra="ignore")


class InvestigateParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    guest_name: Optional[str] = None
    date: Optional[_date] = None
    room: Optional[str] = None

    @model_validator(mode="after")
    def _clean_and_check(self):
        # 先剥空白：纯空格的 guest_name/room 视为空，否则 ilike "%  %" 会匹配全库、强制 ambiguous
        self.guest_name = (self.guest_name or "").strip() or None
        self.room = (self.room or "").strip() or None
        if not (self.guest_name or self.date or self.room):
            raise ValueError("至少要有客人姓名/日期/房间之一才能定位订单")
        return self


# ─── Handlers（只读；查数复用现成对齐口径函数）────────────────────────────────

async def _h_period_metrics(db, params: PeriodParams, current_user: dict) -> dict:
    from app.services.dashboard_metrics import compute_period_metrics
    start, end, label = resolve_period(params.period)
    m = await compute_period_metrics(db, start, end)
    monthly_breakdown = []
    cursor_year, cursor_month = start.year, start.month
    while (cursor_year, cursor_month) <= (end.year, end.month):
        month_start = _date(cursor_year, cursor_month, 1)
        month_end = _date(cursor_year, cursor_month, calendar.monthrange(cursor_year, cursor_month)[1])
        bucket = await compute_period_metrics(db, max(start, month_start), min(end, month_end))
        monthly_breakdown.append({
            "month": f"{cursor_year}-{cursor_month:02d}",
            "period_label": f"{cursor_year}年{cursor_month}月",
            "order_count": bucket["order_count"],
            "revenue": str(bucket["revenue"]),
            "commission": str(bucket["commission"]),
            "net_revenue": str(bucket["revenue"] - bucket["commission"]),
            "room_nights": bucket["room_nights"],
        })
        if cursor_month == 12:
            cursor_year, cursor_month = cursor_year + 1, 1
        else:
            cursor_month += 1

    return {
        "period_label": label,
        "order_count": m["order_count"],
        "revenue": str(m["revenue"]),
        "commission": str(m["commission"]),
        "net_revenue": str(m["revenue"] - m["commission"]),
        "room_nights": m["room_nights"],
        "monthly_breakdown": monthly_breakdown,
        "by_channel": {
            ch: {"order_count": v["order_count"], "revenue": str(v["revenue"])}
            for ch, v in m["by_channel"].items()
        },
    }


async def _h_order_count(db, params: PeriodParams, current_user: dict) -> dict:
    from app.services.dashboard_metrics import compute_period_metrics
    start, end, label = resolve_period(params.period)
    m = await compute_period_metrics(db, start, end)
    return {"period_label": label, "order_count": m["order_count"]}


async def _h_owner_earnings(db, params: PeriodParams, current_user: dict) -> dict:
    from collections import defaultdict
    from decimal import Decimal
    from sqlalchemy import select
    from app.models.owner import Owner
    from app.models.settlement import OwnerSettlement

    start, end, label = resolve_period(params.period)
    rows = (await db.execute(
        select(OwnerSettlement, Owner.name)
        .join(Owner, Owner.owner_id == OwnerSettlement.owner_id)
        .where(
            OwnerSettlement.billing_month >= start.strftime("%Y-%m"),
            OwnerSettlement.billing_month <= end.strftime("%Y-%m"),
        )
        .order_by(Owner.name, OwnerSettlement.billing_month)
    )).all()
    grouped: dict[str, dict] = {}
    statuses: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for settlement, owner_name in rows:
        item = grouped.setdefault(settlement.owner_id, {
            "owner_id": settlement.owner_id, "owner_name": owner_name,
            "settlement_count": 0, "owner_amount": Decimal("0"),
            "deducted_expenses": Decimal("0"), "actual_owner_amount": Decimal("0"),
        })
        item["settlement_count"] += 1
        item["owner_amount"] += Decimal(settlement.owner_amount or 0)
        item["deducted_expenses"] += Decimal(settlement.deducted_expenses or 0)
        item["actual_owner_amount"] += Decimal(settlement.actual_owner_amount or 0)
        status = settlement.status.value if hasattr(settlement.status, "value") else str(settlement.status)
        statuses[settlement.owner_id][status] += 1

    owners = []
    for owner_id, item in grouped.items():
        owners.append({
            **item,
            "owner_amount": f"{item['owner_amount']:.2f}",
            "deducted_expenses": f"{item['deducted_expenses']:.2f}",
            "actual_owner_amount": f"{item['actual_owner_amount']:.2f}",
            "status_counts": dict(statuses[owner_id]),
        })
    owners.sort(key=lambda row: row["owner_name"])
    total = sum((Decimal(row["actual_owner_amount"]) for row in owners), Decimal("0"))
    return {"period_label": label, "owners": owners, "total_actual_owner_amount": f"{total:.2f}"}


async def _h_room_revenue(db, params: RoomRevenueParams, current_user: dict) -> dict:
    from app.api.v1.finance import summary_by_room
    start, end, label = resolve_period(params.period)
    rows = await summary_by_room(db=db, current_user=current_user, start_date=start, end_date=end)
    items = [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in rows]
    if params.room_id:
        items = [r for r in items if str(r.get("room_id")) == params.room_id]
    return {"period_label": label, "room_id": params.room_id, "rooms": items}


async def _h_cleaning_workload(db, params: CleaningWorkloadParams, current_user: dict) -> dict:
    from sqlalchemy import func, select
    from app.models.task import Task, TaskStatus, TaskType
    from app.models.user import User

    start, end, label = resolve_period(params.period)
    stmt = (
        select(Task.assignee_id, func.count())
        .where(
            Task.task_type == TaskType.cleaning,
            Task.status == TaskStatus.done,
            func.date(Task.completed_at) >= start,
            func.date(Task.completed_at) <= end,
        )
        .group_by(Task.assignee_id)
    )
    rows = (await db.execute(stmt)).all()
    assignee_ids = {aid for aid, _ in rows if aid}
    name_map: dict[str, str] = {}
    if assignee_ids:
        users = (await db.execute(select(User).where(User.user_id.in_(assignee_ids)))).scalars().all()
        name_map = {u.user_id: u.display_name for u in users}

    per_cleaner = []
    total = 0
    for aid, cnt in rows:
        total += cnt
        name = name_map.get(aid, "未指派" if aid is None else aid)
        per_cleaner.append({"cleaner": name, "rooms_cleaned": cnt})

    if params.cleaner:
        per_cleaner = [c for c in per_cleaner if params.cleaner in str(c["cleaner"])]

    return {"period_label": label, "total_rooms_cleaned": total, "per_cleaner": per_cleaner}


async def _h_today_snapshot(db, params: NoParams, current_user: dict) -> dict:
    from app.api.v1.dashboard import _today_stats
    return await _today_stats(db)


async def _h_investigate_order(db, params: InvestigateParams, current_user: dict) -> dict:
    from app.services.assistant.forensics import investigate_order
    return await investigate_order(
        db, guest_name=params.guest_name, on_date=params.date, room=params.room
    )


# ─── 白名单注册表 ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    param_model: Type[BaseModel]
    handler: Callable[..., Any]
    kind: str  # "forensics" | "metrics"


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "investigate_order": ToolSpec(InvestigateParams, _h_investigate_order, "forensics"),
    "period_metrics": ToolSpec(PeriodParams, _h_period_metrics, "metrics"),
    "room_revenue": ToolSpec(RoomRevenueParams, _h_room_revenue, "metrics"),
    "order_count": ToolSpec(PeriodParams, _h_order_count, "metrics"),
    "cleaning_workload": ToolSpec(CleaningWorkloadParams, _h_cleaning_workload, "metrics"),
    "today_snapshot": ToolSpec(NoParams, _h_today_snapshot, "metrics"),
    "owner_earnings": ToolSpec(PeriodParams, _h_owner_earnings, "metrics"),
}


@dataclass(frozen=True)
class ValidatedCall:
    tool: str
    params: BaseModel
    spec: ToolSpec


def validate_tool_call(tool: str, params: dict | None) -> ValidatedCall:
    """白名单 + pydantic 校验（纯，无 DB）。tool 越界 → UnknownToolError；参数不合规 → ToolParamError。"""
    spec = TOOL_REGISTRY.get(tool)
    if spec is None:
        raise UnknownToolError(f"工具不在白名单：{tool!r}")
    try:
        validated = spec.param_model.model_validate(params or {})
    except ValidationError as e:
        raise ToolParamError(str(e)) from e
    return ValidatedCall(tool=tool, params=validated, spec=spec)


async def dispatch(db, call: ValidatedCall, current_user: dict) -> dict:
    """执行已校验的工具调用。周期解析失败（含糊词）→ ToolParamError，交上层回显/拒答。"""
    try:
        return await call.spec.handler(db, call.params, current_user)
    except PeriodError as e:
        raise ToolParamError(str(e)) from e
