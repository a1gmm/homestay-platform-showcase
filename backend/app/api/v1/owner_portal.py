"""
Owner portal (业主自助端) API.

Auth:
- POST /owner/auth/send-otp        — reuse SMS OTP infra
- POST /owner/auth/verify          — verify OTP, check phone exists in owners, issue owner JWT
- GET  /owner/me                   — owner info + rooms count

Data (all scoped by current_owner_id):
- GET /owner/me/summary?year&month     — aggregated monthly stats
- GET /owner/me/rooms?year&month       — per-room monthly stats
- GET /owner/me/rooms/{room_id}?year&month — room detail: orders + finance
- GET /owner/me/settlements            — my monthly settlements list
- GET /owner/me/settlements/{id}       — settlement detail with items
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from sqlalchemy import select, func, extract
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, Field, field_validator
from dataclasses import dataclass
from decimal import Decimal
from datetime import date, datetime, timezone, timedelta
from typing import Annotated, Optional
import enum
import re

from app.core.deps import DBSession, RedisClient, CurrentOwnerId
from app.core.rate_limit import enforce_rate_limit
from app.core.security import create_owner_token, hash_password, verify_password
from app.core.datetime_helpers import today_cn
from app.models.owner import Owner
from app.models.room import Room
from app.models.order import Order, OrderStatus
from app.models.room_block import RoomBlock
from app.models.settlement import OwnerSettlement
from app.services import otp_service, sms_service
from app.services.owner_scope import get_scoped_owner_ids, get_sub_owners
from app.services.service_fees import get_service_fees
from app.api.v1.rooms import (
    RoomImageOut,
    RoomImagePatch,
    RoomImageReorder,
    upload_image_for_room,
    patch_image,
    delete_image_for_room,
    reorder_images_for_room,
    _list_images as list_room_images_helper,
)

router = APIRouter(prefix="/owner", tags=["owner-portal"])


# ─── Schemas ─────────────────────────────────────────────────────────────────

class OwnerSendOtp(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)

    def validate_phone(self) -> None:
        if not re.fullmatch(r"1[3-9]\d{9}", self.phone):
            raise ValueError("手机号格式不正确")


class OwnerVerifyOtp(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=6)


class OwnerPasswordLogin(BaseModel):
    username: str = Field(..., min_length=4, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)


class OwnerChangePassword(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码长度不能少于8位")
        if v.isdigit() or v.isalpha():
            raise ValueError("密码必须包含字母和数字")
        return v


class SubOwnerBrief(BaseModel):
    owner_id: str
    name: str
    room_count: int


class OwnerProfile(BaseModel):
    owner_id: str
    name: str
    username: Optional[str]
    phone: Optional[str]
    room_count: int
    is_master: bool = False
    sub_owners: list[SubOwnerBrief] = []


class OwnerToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    owner: OwnerProfile


class RoomMonthlyStat(BaseModel):
    room_id: str
    room_name: str
    room_type: Optional[str] = None  # 用于业主端按房型分组展示
    owner_id: str = ""      # 总账号按业主(=楼层)分组用
    owner_name: str = ""    # 总账号按业主(=楼层)分组用
    owner_share_ratio: Decimal
    order_count: int
    occupancy_nights: int  # 本月已住晚数（近似 = 订单 nights 和，不 clip 到本月）
    # 金额字段可为 None：hide_amounts 账号（如脱敏演示号）后端一律置 None（真数字不出后端）。
    revenue: Optional[Decimal] = None
    net_revenue: Optional[Decimal] = None
    owner_expenses: Optional[Decimal] = None
    owner_net: Optional[Decimal] = None  # (net - owner_expenses) * share_ratio


class MonthlySummary(BaseModel):
    year: int
    month: int
    billing_month: str
    rooms_total: int
    order_count: int
    # 金额字段可为 None：hide_amounts 账号后端置 None。
    revenue: Optional[Decimal] = None
    net_revenue: Optional[Decimal] = None
    owner_expenses: Optional[Decimal] = None
    owner_net: Optional[Decimal] = None
    # 「结算到昨天」截止日：仅当查看当前月时返回（=昨天），供前端提示业主这是
    # 阶段性数字、本月未退房订单退房后陆续计入；查看历史月为 null（整月已终结）。
    as_of_date: Optional[date] = None
    rooms: list[RoomMonthlyStat]


class OrderLite(BaseModel):
    order_id: str
    channel: str
    guest_name: str
    check_in_date: date
    check_out_date: date
    nights: int
    actual_price: Optional[Decimal]
    commission_rate: Decimal
    order_status: str


class RoomDetail(BaseModel):
    room_id: str
    room_name: str
    year: int
    month: int
    stat: RoomMonthlyStat
    orders: list[OrderLite]


class SettlementBrief(BaseModel):
    settlement_id: str
    owner_id: str = ""      # 总账号按业主(=楼层)分组用
    owner_name: str = ""
    billing_month: str
    # hide_amounts 账号后端置 None。
    actual_owner_amount: Optional[Decimal] = None
    # 账面「到厘」展示值（打款仍用 actual_owner_amount 到分值）
    actual_owner_amount_precise: Optional[Decimal] = None
    status: str
    created_at: str


class SettlementItemDetail(BaseModel):
    item_id: str
    # 业主级支出行（整层/某业主电费）room_id 为空，前端用 label 展示。
    room_id: Optional[str] = None
    room_name: Optional[str] = None
    label: Optional[str] = None
    order_count: int
    # hide_amounts 账号后端置 None（share_ratio_snapshot 是比例非金额，保留）。
    revenue: Optional[Decimal] = None
    commission: Optional[Decimal] = None
    net_revenue: Optional[Decimal] = None
    externally_settled_income: Optional[Decimal] = None
    sponsorship_adjustment_id: Optional[str] = None
    owner_expenses: Optional[Decimal] = None
    share_ratio_snapshot: Decimal
    owner_net_amount: Optional[Decimal] = None
    # 该房「业主应得·到厘」展示值（打款仍用 owner_net_amount 到分值）
    owner_net_amount_precise: Optional[Decimal] = None
    # v2#4/#5: 每笔费用按 RoomCostShareRule 加权后的明细，业主端展示费用拆解
    cost_share_breakdown: list = []


class SettlementDetail(BaseModel):
    settlement_id: str
    billing_month: str
    # hide_amounts 账号后端置 None。
    total_net_revenue: Optional[Decimal] = None
    owner_amount: Optional[Decimal] = None
    deducted_expenses: Optional[Decimal] = None
    actual_owner_amount: Optional[Decimal] = None
    # 账面「到厘」展示值（打款仍用上面到分的 owner_amount/actual_owner_amount）
    owner_amount_precise: Optional[Decimal] = None
    actual_owner_amount_precise: Optional[Decimal] = None
    status: str
    notes: Optional[str]
    # 当前服务费费率。业主端据此把金额还原成「¥65 × 85次」；缺失或对不上时前端
    # 降级成「N笔」。刻意不存历史快照（CEO review 2026-07-16）：费率从未改过，
    # 且快照救不了已生成的存量单。改费率后老单会降级——不好看，但不会显示错的单价。
    service_fee_rates: dict = {}
    items: list[SettlementItemDetail]


# 业主自助房态：故意精简，剥掉一切 PII（guest_name / guest_phone / 价格 / 收款），
# 业主仅看到「占用状态 + 渠道」满足"判断房间是否真有人住"的诉求，避免《个保法》风险。
class OwnerCalendarDay(BaseModel):
    status: str  # OrderStatus.value 或 "block:{block_type}"
    channel: Optional[str] = None  # ctrip / meituan / tujia / private / walk_in / direct
    is_check_in_day: bool = False
    is_check_out_day: bool = False


class OwnerCalendarRoom(BaseModel):
    room_id: str
    room_name: str
    room_type: Optional[str] = None
    floor: Optional[int] = None
    owner_id: str = ""      # 总账号按业主(=楼层)给日历分块用
    owner_name: str = ""
    room_status: str
    days: dict[str, OwnerCalendarDay]


async def _build_owner_profile(db, owner: Owner) -> OwnerProfile:
    """构造 profile。总账号（有子业主）→ 聚合各子业主房间数并列出子业主。"""
    subs = await get_sub_owners(db, owner.owner_id)
    if not subs:
        return OwnerProfile(
            owner_id=owner.owner_id, name=owner.name, username=owner.username,
            phone=owner.phone, room_count=len(owner.rooms or []),
            is_master=False, sub_owners=[],
        )
    briefs: list[SubOwnerBrief] = []
    total = len(owner.rooms or [])  # 总账号一般为 0，但若自身也挂了房则一并计入
    for s in subs:
        cnt = (await db.execute(
            select(func.count()).select_from(Room).where(Room.owner_id == s.owner_id)
        )).scalar_one()
        briefs.append(SubOwnerBrief(owner_id=s.owner_id, name=s.name, room_count=cnt))
        total += cnt
    return OwnerProfile(
        owner_id=owner.owner_id, name=owner.name, username=owner.username,
        phone=owner.phone, room_count=total, is_master=True, sub_owners=briefs,
    )


# ─── Auth ────────────────────────────────────────────────────────────────────

@router.post("/auth/send-otp")
async def owner_send_otp(body: OwnerSendOtp, request: Request, db: DBSession, redis: RedisClient):
    try:
        body.validate_phone()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 业主端 OTP 前先校验手机号已被管理员预录，否则直接拒绝，避免浪费短信
    exists = await db.execute(select(Owner.owner_id).where(Owner.phone == body.phone))
    if not exists.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="该手机号未绑定业主账号,请联系管理员")

    ip = request.client.host if request.client else None
    code = await otp_service.generate_and_store(redis, body.phone, ip)
    try:
        await sms_service.send_otp_sms(body.phone, code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"短信发送失败：{e}")
    return {"sent": True}


@router.post("/auth/login-password", response_model=OwnerToken)
async def owner_login_password(
    body: OwnerPasswordLogin, request: Request, db: DBSession, redis: RedisClient,
):
    """业主账号密码登录(账号 = owners.username,大小写不敏感)。"""
    username = body.username.strip().lower()
    # 密码爆破防线（批5）：此前业主密码登录完全无限流，可无限尝试。
    # 按账号+IP 双维度限流：单账号 10 次/5 分钟、单 IP 30 次/5 分钟。Redis 挂时 fail-open（会告警）。
    ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(redis, f"owner_login:user:{username}", limit=10, window_seconds=300,
                             detail="登录尝试过于频繁，请 5 分钟后再试")
    await enforce_rate_limit(redis, f"owner_login:ip:{ip}", limit=30, window_seconds=300,
                             detail="登录尝试过于频繁，请 5 分钟后再试")
    result = await db.execute(
        select(Owner).options(selectinload(Owner.rooms)).where(Owner.username == username)
    )
    owner = result.scalar_one_or_none()
    if not owner or not owner.password_hash:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    if not verify_password(body.password, owner.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    token, expires_in = create_owner_token(owner.owner_id)
    return OwnerToken(
        access_token=token,
        expires_in=expires_in,
        owner=await _build_owner_profile(db, owner),
    )


@router.post("/auth/verify", response_model=OwnerToken)
async def owner_verify_otp(body: OwnerVerifyOtp, db: DBSession, redis: RedisClient):
    ok = await otp_service.verify(redis, body.phone, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    result = await db.execute(
        select(Owner).options(selectinload(Owner.rooms)).where(Owner.phone == body.phone)
    )
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="该手机号未绑定业主账号,请联系管理员")

    token, expires_in = create_owner_token(owner.owner_id)
    return OwnerToken(
        access_token=token,
        expires_in=expires_in,
        owner=await _build_owner_profile(db, owner),
    )


# ─── 镜像视图 + 隐藏金额 上下文 ───────────────────────────────────────────────
# view_as_owner_id：登录账号看「被镜像业主」的房间/数据（演示号）。
# hide_amounts：把所有金额字段后端置 None（真数字不出后端）。两者独立、可单用。

@dataclass
class OwnerViewContext:
    auth_owner_id: str   # 登录账号本身（改密码等定位它）
    owner_id: str        # 取数用的有效业主（view_as 时 = 被镜像业主，否则 = 自己）
    hide_amounts: bool
    hide_guests: bool


async def _owner_view_context(owner_id: CurrentOwnerId, db: DBSession) -> OwnerViewContext:
    owner = await db.get(Owner, owner_id)
    if not owner:
        raise HTTPException(status_code=401, detail="登录已过期")
    effective = owner.view_as_owner_id or owner.owner_id
    return OwnerViewContext(
        auth_owner_id=owner.owner_id,
        owner_id=effective,
        hide_amounts=bool(owner.hide_amounts),
        hide_guests=bool(owner.hide_guests),
    )


# 客人姓名脱敏占位（演示号 hide_guests 时用）
REDACTED_GUEST_NAME = "客人"


OwnerView = Annotated[OwnerViewContext, Depends(_owner_view_context)]


# ── 金额脱敏：置 None（前端渲染成「——」）。参照 dashboard._redact_* 的做法。 ──

def _redact_room_stat(r: "RoomMonthlyStat") -> None:
    r.revenue = r.net_revenue = r.owner_expenses = r.owner_net = None


def _redact_summary(s: "MonthlySummary") -> None:
    s.revenue = s.net_revenue = s.owner_expenses = s.owner_net = None
    for r in s.rooms:
        _redact_room_stat(r)


def _redact_order_lite(o: "OrderLite") -> None:
    o.actual_price = None  # commission_rate 是比例非金额，保留


def _redact_room_detail(d: "RoomDetail") -> None:
    _redact_room_stat(d.stat)
    for o in d.orders:
        _redact_order_lite(o)


def _redact_settlement_brief(s: "SettlementBrief") -> None:
    s.actual_owner_amount = None
    s.actual_owner_amount_precise = None


def _redact_settlement_detail(d: "SettlementDetail") -> None:
    d.total_net_revenue = d.owner_amount = d.deducted_expenses = None
    d.actual_owner_amount = None
    d.owner_amount_precise = d.actual_owner_amount_precise = None
    for it in d.items:
        it.revenue = it.commission = it.net_revenue = None
        it.externally_settled_income = it.owner_expenses = None
        it.owner_net_amount = None
        it.owner_net_amount_precise = None
        it.cost_share_breakdown = []


@router.get("/me", response_model=OwnerProfile)
async def owner_me(ctx: OwnerView, db: DBSession):
    # 镜像账号（view_as）返回被镜像业主的资料，让业主端头部/房间数看起来就是那位业主。
    result = await db.execute(
        select(Owner).options(selectinload(Owner.rooms)).where(Owner.owner_id == ctx.owner_id)
    )
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="业主不存在")
    return await _build_owner_profile(db, owner)


@router.post("/me/password")
async def owner_change_password(
    body: OwnerChangePassword, owner_id: CurrentOwnerId, db: DBSession
):
    """业主自助修改密码（需提供当前密码）。"""
    result = await db.execute(select(Owner).where(Owner.owner_id == owner_id))
    owner = result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="业主不存在")
    if not owner.password_hash or not verify_password(body.current_password, owner.password_hash):
        raise HTTPException(status_code=401, detail="当前密码错误")

    owner.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"message": "密码已修改"}


# ─── Data: summary / rooms / room detail ─────────────────────────────────────

def _settlement_cutoff() -> date:
    """业主门户「结算到昨天」截止日 = 北京时间昨天（2026-07-14 王总拍板）。

    业主端只展示离店日 ≤ 昨天的收入：本月未退房走人的单不提前算给业主，
    退房后自动计入。看历史月时昨天已晚于该月，整月照显、不被截断。
    注意：此 cutoff 只用于**业主门户展示**——月底真实结算单生成不传 cutoff。"""
    return today_cn() - timedelta(days=1)


async def _compute_room_stats(
    db, owner_ids: list[str], year: int, month: int
) -> list[RoomMonthlyStat]:
    """
    聚合作用域内所有房间当月统计（订单维度）。owner_ids = [自己]或[自己+子业主]。
    逻辑基本复用 finance/summary/by-room 和 settlements 生成流程。

    业主门户口径：只统计离店日 ≤ 昨天的订单（「结算到昨天」，见 _settlement_cutoff）。
    """
    rooms_result = await db.execute(
        select(Room).where(Room.owner_id.in_(owner_ids)).order_by(Room.room_id)
    )
    rooms = rooms_result.scalars().all()
    if not rooms:
        return []

    # 作用域内业主名映射（打标签用）
    owner_name_map = dict(
        (await db.execute(
            select(Owner.owner_id, Owner.name).where(Owner.owner_id.in_(owner_ids))
        )).all()
    )

    # 单房单月分成计算统一走共享服务（与结算单生成同一口径：v2 公式、
    # RoomCostShareRule 费用加权、ota_subsidy 补贴、每房佣金覆盖——禁止在这里内联算）。
    from app.services.owner_settlement import (
        compute_room_month_owner_stat,
        load_room_sponsorship_income,
    )

    cutoff = _settlement_cutoff()
    sponsorship_income_by_room = await load_room_sponsorship_income(
        db, [room.room_id for room in rooms], year, month, cutoff=cutoff
    )
    out: list[RoomMonthlyStat] = []
    for room in rooms:
        stat = await compute_room_month_owner_stat(
            db,
            room,
            year,
            month,
            cutoff=cutoff,
            sponsorship_income_by_room=sponsorship_income_by_room,
        )
        out.append(
            RoomMonthlyStat(
                room_id=room.room_id,
                room_name=room.room_name,
                room_type=room.room_type,
                owner_id=room.owner_id or "",
                owner_name=owner_name_map.get(room.owner_id, ""),
                owner_share_ratio=stat.share_ratio,
                order_count=stat.order_count,
                occupancy_nights=stat.nights,
                revenue=stat.revenue.quantize(Decimal("0.01")),
                net_revenue=stat.net_revenue.quantize(Decimal("0.01")),
                owner_expenses=stat.owner_expenses.quantize(Decimal("0.01")),
                owner_net=stat.owner_net,
            )
        )
    return out


def _default_year_month() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    return now.year, now.month


@router.get("/me/summary", response_model=MonthlySummary)
async def owner_monthly_summary(
    ctx: OwnerView,
    db: DBSession,
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None, ge=1, le=12),
):
    y, m = (year, month) if year and month else _default_year_month()
    scope = await get_scoped_owner_ids(db, ctx.owner_id)
    rooms = await _compute_room_stats(db, scope, y, m)
    # 顶部「订单数」用去重订单数：一张多房订单只算一次，避免各房叠加虚高
    # （与主系统首页口径一致；每房卡里仍显示该房自己的订单数）。
    cutoff = _settlement_cutoff()
    from app.models.order_room import OrderRoom
    distinct_orders = (await db.execute(
        select(func.count(func.distinct(OrderRoom.order_id)))
        .join(Order, Order.order_id == OrderRoom.order_id)
        .join(Room, Room.room_id == OrderRoom.room_id)
        .where(Room.owner_id.in_(scope))
        .where(Order.is_deleted == False)
        .where(Order.order_status != OrderStatus.cancelled)
        # 月度归属按离店日期（2026-07-14 拍板），与业主分账口径一致
        .where(extract("year", OrderRoom.check_out_date) == y)
        .where(extract("month", OrderRoom.check_out_date) == m)
        # 「结算到昨天」：只数已退房走人的单，与上方分成数字口径一致
        .where(OrderRoom.check_out_date <= cutoff)
    )).scalar_one()
    # 业主级支出（整层/某业主录入，room_id 空、挂 owner_id）——不属于任何单间，
    # 逐房求和漏掉，须按 scope 内每个业主单独加总，整笔 100% 从分成扣（方案 B），
    # 让实时预计与月底结算单一致。cutoff 同逐房口径（结算到昨天）。
    from app.services.owner_settlement import compute_owner_level_expenses
    owner_level_total = Decimal("0")
    for oid in scope:
        ol = await compute_owner_level_expenses(db, oid, y, m, cutoff=cutoff)
        owner_level_total += ol.total_owner_borne

    # 说明只在查看「当前月」时给（阶段性数字）；历史月已终结不需提示。
    today = today_cn()
    as_of_date = cutoff if (y == today.year and m == today.month) else None
    result = MonthlySummary(
        year=y,
        month=m,
        billing_month=f"{y}-{str(m).zfill(2)}",
        rooms_total=len(rooms),
        order_count=distinct_orders,
        as_of_date=as_of_date,
        revenue=sum((r.revenue for r in rooms), Decimal("0")),
        net_revenue=sum((r.net_revenue for r in rooms), Decimal("0")),
        owner_expenses=sum((r.owner_expenses for r in rooms), Decimal("0")) + owner_level_total,
        owner_net=sum((r.owner_net for r in rooms), Decimal("0")) - owner_level_total,
        rooms=rooms,
    )
    if ctx.hide_amounts:
        _redact_summary(result)
    return result


@router.get("/me/rooms", response_model=list[RoomMonthlyStat])
async def owner_rooms(
    ctx: OwnerView,
    db: DBSession,
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None, ge=1, le=12),
):
    y, m = (year, month) if year and month else _default_year_month()
    stats = await _compute_room_stats(db, await get_scoped_owner_ids(db, ctx.owner_id), y, m)
    if ctx.hide_amounts:
        for s in stats:
            _redact_room_stat(s)
    return stats


@router.get("/me/rooms/{room_id}", response_model=RoomDetail)
async def owner_room_detail(
    room_id: str,
    ctx: OwnerView,
    db: DBSession,
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None, ge=1, le=12),
):
    y, m = (year, month) if year and month else _default_year_month()

    # 校验房间归属当前业主作用域（自己或——若为总账号——子业主）
    scope = await get_scoped_owner_ids(db, ctx.owner_id)
    room = (await db.execute(
        select(Room).where(Room.room_id == room_id, Room.owner_id.in_(scope))
    )).scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房源不存在或非你名下")

    stats = await _compute_room_stats(db, scope, y, m)
    stat = next((s for s in stats if s.room_id == room_id), None)
    if stat is None:
        stat = RoomMonthlyStat(
            room_id=room.room_id, room_name=room.room_name,
            owner_share_ratio=Decimal(str(room.owner_share_ratio or 0)),
            order_count=0, occupancy_nights=0,
            revenue=Decimal("0"), net_revenue=Decimal("0"),
            owner_expenses=Decimal("0"), owner_net=Decimal("0"),
        )

    # multi-room: 通过 OrderRoom 关联，取该房间在本月的所有订单行（同一订单的多房中只有
    # 命中本 room_id 的那一行进入业主月报；金额、入退日用 OrderRoom 上的字段）
    from app.models.order_room import OrderRoom
    rows_result = await db.execute(
        select(Order, OrderRoom)
        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
        .where(OrderRoom.room_id == room_id)
        .where(Order.is_deleted == False)
        .where(Order.order_status != OrderStatus.cancelled)
        # 月度归属按离店日期（2026-07-14 拍板），与业主分账口径一致
        .where(extract("year", OrderRoom.check_out_date) == y)
        .where(extract("month", OrderRoom.check_out_date) == m)
        # 「结算到昨天」：明细只列已退房走人的单，与本房分成数字对得上
        .where(OrderRoom.check_out_date <= _settlement_cutoff())
        .order_by(OrderRoom.check_out_date.desc())
    )
    rows = rows_result.all()

    order_items = [
        OrderLite(
            order_id=o.order_id,
            channel=o.channel.value if hasattr(o.channel, "value") else str(o.channel),
            guest_name=REDACTED_GUEST_NAME if ctx.hide_guests else o.guest_name,
            check_in_date=orm.check_in_date,
            check_out_date=orm.check_out_date,
            nights=orm.nights,
            actual_price=orm.actual_price,
            commission_rate=o.platform_commission_rate or Decimal("0"),
            order_status=o.order_status.value if hasattr(o.order_status, "value") else str(o.order_status),
        )
        for (o, orm) in rows
    ]

    detail = RoomDetail(
        room_id=room.room_id, room_name=room.room_name,
        year=y, month=m, stat=stat, orders=order_items,
    )
    if ctx.hide_amounts:
        _redact_room_detail(detail)
    return detail


# ─── Calendar (业主自助房态：仅展示占用状态 + 渠道，不下发任何 PII) ─────────────

@router.get("/me/calendar", response_model=list[OwnerCalendarRoom])
async def owner_calendar(
    ctx: OwnerView,
    db: DBSession,
    year: int = Query(default=None),
    month: int = Query(default=None),
):
    """业主自助房态视图：基于 /availability/calendar 的逻辑，但
    1. 仅返回 owner_id 名下房间；
    2. 剥掉 guest_name / guest_phone / actual_price / payment_status / order_id 等敏感字段；
    3. 加 is_check_in_day / is_check_out_day 标记（让前端能在不知道客人是谁的前提下
       仍能高亮"今天有人入/退"）。
    """
    from calendar import monthrange

    if year is None or month is None:
        t = today_cn()
        year = year or t.year
        month = month or t.month
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="月份不合法")

    _, days_in_month = monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    # 1. 业主名下所有房（防越权的核心 WHERE 条件；总账号可看作用域内子业主的房）
    scope = await get_scoped_owner_ids(db, ctx.owner_id)
    rooms_result = await db.execute(
        select(Room).where(Room.owner_id.in_(scope)).order_by(Room.room_id)
    )
    rooms = rooms_result.scalars().all()
    if not rooms:
        return []
    owned_room_ids = {r.room_id for r in rooms}

    # 2. 该些房间在本月所有有效订单（multi-room: 按 OrderRoom 粒度遍历，
    #    多房订单只把命中本业主房间的那行画到日历）
    from app.models.order_room import OrderRoom
    rows_result = await db.execute(
        select(Order, OrderRoom)
        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
        .where(
            OrderRoom.room_id.in_(owned_room_ids),
            Order.is_deleted == False,
            Order.order_status != OrderStatus.cancelled,
            OrderRoom.check_in_date <= month_end,
            OrderRoom.check_out_date >= month_start,
        )
    )
    order_room_pairs = rows_result.all()

    # 3. 同范围内 RoomBlock（房东自用 / 维修等）
    blocks_result = await db.execute(
        select(RoomBlock).where(
            RoomBlock.room_id.in_(owned_room_ids),
            RoomBlock.start_date <= month_end,
            RoomBlock.end_date >= month_start,
        )
    )
    blocks = blocks_result.scalars().all()

    # 作用域内业主名映射（总账号按楼层分块用；单业主时只有自己一条）
    owner_name_map = dict(
        (await db.execute(
            select(Owner.owner_id, Owner.name).where(Owner.owner_id.in_(scope))
        )).all()
    )

    # 4. 装填日历，故意只放允许字段（含 owner 标签，供总账号按楼层分块；仍不含任何 PII）
    calendar: dict[str, OwnerCalendarRoom] = {}
    for r in rooms:
        calendar[r.room_id] = OwnerCalendarRoom(
            room_id=r.room_id,
            room_name=r.room_name,
            room_type=r.room_type,
            floor=r.floor,
            owner_id=r.owner_id or "",
            owner_name=owner_name_map.get(r.owner_id, ""),
            room_status=r.room_status.value if hasattr(r.room_status, "value") else str(r.room_status),
            days={},
        )

    for order, orm in order_room_pairs:
        channel_value = order.channel.value if hasattr(order.channel, "value") else str(order.channel)
        status_value = order.order_status.value if hasattr(order.order_status, "value") else str(order.order_status)
        # 用 OrderRoom 上的日期 + room_id（多房订单各房日期可不同）
        current = max(orm.check_in_date, month_start)
        end = min(orm.check_out_date, month_end + timedelta(days=1))
        while current < end:
            calendar[orm.room_id].days[current.isoformat()] = OwnerCalendarDay(
                status=status_value,
                channel=channel_value,
                is_check_in_day=(current == orm.check_in_date),
                is_check_out_day=(current == orm.check_out_date - timedelta(days=1)),
            )
            current += timedelta(days=1)

    for block in blocks:
        block_type_value = (
            block.block_type.value if isinstance(block.block_type, enum.Enum) else str(block.block_type)
        )
        current = max(block.start_date, month_start)
        end = min(block.end_date, month_end + timedelta(days=1))
        while current < end:
            calendar[block.room_id].days[current.isoformat()] = OwnerCalendarDay(
                status=f"block:{block_type_value}",
                channel=None,
                is_check_in_day=False,
                is_check_out_day=False,
            )
            current += timedelta(days=1)

    return list(calendar.values())


# ─── Settlements (my own) ────────────────────────────────────────────────────

@router.get("/me/settlements", response_model=list[SettlementBrief])
async def owner_settlements(ctx: OwnerView, db: DBSession):
    scope = await get_scoped_owner_ids(db, ctx.owner_id)
    owner_name_map = dict(
        (await db.execute(
            select(Owner.owner_id, Owner.name).where(Owner.owner_id.in_(scope))
        )).all()
    )
    result = await db.execute(
        select(OwnerSettlement)
        .options(selectinload(OwnerSettlement.items))
        .where(OwnerSettlement.owner_id.in_(scope))
        .order_by(OwnerSettlement.billing_month.desc())
    )
    items = result.scalars().all()
    from app.services.owner_settlement import (
        settlement_current_amounts,
        settlement_precise_amounts,
    )
    briefs = []
    for s in items:
        _, actual_precise = settlement_precise_amounts(s.items)
        current = settlement_current_amounts(s)
        briefs.append(SettlementBrief(
            settlement_id=s.settlement_id,
            owner_id=s.owner_id,
            owner_name=owner_name_map.get(s.owner_id, ""),
            billing_month=s.billing_month,
            actual_owner_amount=current.actual_owner_amount,
            actual_owner_amount_precise=actual_precise,
            status=s.status.value if hasattr(s.status, "value") else str(s.status),
            created_at=s.created_at.isoformat() if s.created_at else "",
        ))
    if ctx.hide_amounts:
        for b in briefs:
            _redact_settlement_brief(b)
    return briefs


@router.get("/me/settlements/{settlement_id}", response_model=SettlementDetail)
async def owner_settlement_detail(
    settlement_id: str, ctx: OwnerView, db: DBSession,
):
    scope = await get_scoped_owner_ids(db, ctx.owner_id)
    result = await db.execute(
        select(OwnerSettlement)
        .options(selectinload(OwnerSettlement.items))
        .where(OwnerSettlement.settlement_id == settlement_id)
        .where(OwnerSettlement.owner_id.in_(scope))
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="结算单不存在")

    from app.services.owner_settlement import (
        item_precise_owner_net,
        settlement_current_amounts,
        settlement_precise_amounts,
    )

    # room_name lookup（业主级支出行 room_id 为空，跳过）
    room_ids = [i.room_id for i in s.items if i.room_id]
    rooms_map: dict[str, str] = {}
    if room_ids:
        rows = (await db.execute(
            select(Room.room_id, Room.room_name).where(Room.room_id.in_(room_ids))
        )).all()
        rooms_map = {rid: rname for rid, rname in rows}

    items = [
        SettlementItemDetail(
            item_id=i.item_id,
            room_id=i.room_id,
            room_name=rooms_map.get(i.room_id) if i.room_id else None,
            label=getattr(i, "label", None),
            order_count=i.order_count,
            revenue=i.revenue,
            commission=i.commission,
            net_revenue=i.net_revenue,
            externally_settled_income=i.externally_settled_income,
            sponsorship_adjustment_id=i.sponsorship_adjustment_id,
            owner_expenses=i.owner_expenses,
            share_ratio_snapshot=i.share_ratio_snapshot,
            owner_net_amount=i.owner_net_amount,
            owner_net_amount_precise=item_precise_owner_net(i),
            cost_share_breakdown=getattr(i, "cost_share_breakdown", None) or [],
        )
        # 逐房行按房号在前，业主级行（room_id 空）排最后
        for i in sorted(s.items, key=lambda x: (x.room_id is None, x.room_id or ""))
    ]
    owner_amount_precise, actual_owner_amount_precise = settlement_precise_amounts(s.items)
    current = settlement_current_amounts(s)

    # 值转字符串：Decimal 精度不能在 JSON 里丢。get_service_fees 在配置行缺失时
    # 自行回退默认常量，这里拿到的永远是四个有效费率。
    fees = await get_service_fees(db)
    fee_rates = {
        "checkout_cleaning_fee": str(fees.checkout_cleaning_fee),
        "instay_cleaning_fee": str(fees.instay_cleaning_fee),
        "laundry_fee_per_room": str(fees.laundry_fee_per_room),
        "consumable_fee_per_room_night": str(fees.consumable_fee_per_room_night),
    }

    detail = SettlementDetail(
        settlement_id=s.settlement_id,
        billing_month=s.billing_month,
        total_net_revenue=current.total_net_revenue,
        owner_amount=current.owner_amount,
        deducted_expenses=current.deducted_expenses,
        actual_owner_amount=current.actual_owner_amount,
        owner_amount_precise=owner_amount_precise,
        actual_owner_amount_precise=actual_owner_amount_precise,
        status=s.status.value if hasattr(s.status, "value") else str(s.status),
        notes=s.notes,
        service_fee_rates=fee_rates,
        items=items,
    )
    if ctx.hide_amounts:
        _redact_settlement_detail(detail)
    return detail


# ─── Owner-scoped room images ────────────────────────────────────────────────
# Owners may manage images only on rooms assigned to them. Shared CRUD helpers
# from rooms.py are reused to avoid divergence with the admin endpoints.


async def _assert_owner_owns_room(
    db, owner_id: str, room_id: str
) -> Room:
    room = (
        await db.execute(select(Room).where(Room.room_id == room_id))
    ).scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    if room.owner_id != owner_id:
        raise HTTPException(status_code=403, detail="该房间不属于您")
    return room


async def _assert_owner_can_view_room(db, owner_id: str, room_id: str) -> Room:
    """只读校验：作用域内（自己或子业主）的房间可读。总账号看子业主房图片用。"""
    scope = await get_scoped_owner_ids(db, owner_id)
    room = (await db.execute(
        select(Room).where(Room.room_id == room_id, Room.owner_id.in_(scope))
    )).scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在或非你名下")
    return room


@router.get("/me/rooms/{room_id}/images", response_model=list[RoomImageOut])
async def owner_list_room_images(
    room_id: str, db: DBSession, current_owner_id: CurrentOwnerId
):
    await _assert_owner_can_view_room(db, current_owner_id, room_id)
    return await list_room_images_helper(db, room_id)


@router.post(
    "/me/rooms/{room_id}/images",
    response_model=RoomImageOut,
    status_code=201,
)
async def owner_upload_room_image(
    room_id: str,
    db: DBSession,
    current_owner_id: CurrentOwnerId,
    file: UploadFile = File(...),
):
    await _assert_owner_owns_room(db, current_owner_id, room_id)
    content = await file.read()
    image = await upload_image_for_room(
        db,
        room_id=room_id,
        content=content,
        content_type=file.content_type or "",
        size=len(content),
        uploaded_by=None,  # Owner token has no user_id; audit via owner_id separately if needed
    )
    return image


@router.patch(
    "/me/rooms/{room_id}/images/{image_id}", response_model=RoomImageOut
)
async def owner_patch_room_image(
    room_id: str,
    image_id: str,
    body: RoomImagePatch,
    db: DBSession,
    current_owner_id: CurrentOwnerId,
):
    await _assert_owner_owns_room(db, current_owner_id, room_id)
    return await patch_image(db, room_id, image_id, body)


@router.delete("/me/rooms/{room_id}/images/{image_id}")
async def owner_delete_room_image(
    room_id: str,
    image_id: str,
    db: DBSession,
    current_owner_id: CurrentOwnerId,
):
    await _assert_owner_owns_room(db, current_owner_id, room_id)
    await delete_image_for_room(db, room_id, image_id)
    return {"ok": True}


@router.post(
    "/me/rooms/{room_id}/images/reorder", response_model=list[RoomImageOut]
)
async def owner_reorder_room_images(
    room_id: str,
    body: RoomImageReorder,
    db: DBSession,
    current_owner_id: CurrentOwnerId,
):
    await _assert_owner_owns_room(db, current_owner_id, room_id)
    return await reorder_images_for_room(db, room_id, body.ordered_ids)
