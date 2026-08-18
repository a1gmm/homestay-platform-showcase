from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional
import enum

from app.models.company_sponsored_stay import (
    CompanySponsorshipStatus,
    PaymentResponsibility,
)
from app.models.order import (
    BookingType,
    Channel,
    CleaningStatus,
    DepositStatus,
    OrderStatus,
    PaymentStatus,
    StaySettlementKind,
)
from app.services.manual_override import OrderSyncField


_MAX_PRICE = Decimal("10000000")


def _non_negative(v: Optional[Decimal], *, field: str) -> Optional[Decimal]:
    if v is None:
        return v
    if v < 0:
        raise ValueError(f"{field} 不能为负数")
    if v > _MAX_PRICE:
        raise ValueError(f"{field} 超过上限")
    return v


def _validate_commission_rate(v: Optional[Decimal]) -> Optional[Decimal]:
    if v is None:
        return v
    # 统一口径：rate 为小数 0-1（0 = 无佣金，1 = 全额佣金）。前端输入百分比需 /100。
    if v < 0 or v > 1:
        raise ValueError("platform_commission_rate 必须在 0-1 之间（小数，而非百分比）")
    return v


def _validate_phone_format(v: Optional[str]) -> Optional[str]:
    # 空 / None 放行（issue#3 选填）；非空必须是 11 位中国大陆手机号。
    # 创建期(OrderCreate)与编辑期(OrderUpdate)共用，避免改单时写入畸形手机号。
    if v is None or v == "":
        return None
    import re
    if not re.fullmatch(r"1[3-9]\d{9}", v):
        raise ValueError("手机号格式不正确（11 位 1 开头）")
    return v


# ─── issue #103 Step 3：手填 normal 单必填规则（单一真相源）──────────────────────
# 创建期(OrderCreate.model_validator)与编辑期(api/v1/orders.update_order)共用，
# 避免豁免集合 / 必填字段 / 文案在两处漂移。试住(trial)、自住(owner_self)豁免，
# 与 actual_price 校验口径一致；OTA 搬单走 raw SQL 直写库，天然不经此规则。
NORMAL_REQUIRES_PHONE = "普通商单必须填写客人电话"
NORMAL_REQUIRES_LIST_PRICE = "普通商单必须填写每间房的客人价"


def normal_booking_violation(
    booking_type: Optional[BookingType],
    guest_phone: Optional[str],
    rooms,
    *,
    check_phone: bool = True,
    check_list_price: bool = True,
) -> Optional[str]:
    """返回首条违规文案，合规返回 None。

    非 normal 单一律豁免。编辑期按"是否触碰相关字段"决定 check_phone /
    check_list_price，避免阻断仅改 notes 等无关字段的老数据更新。

    注：手机号已改为选填（前台先录单后补，王总要求）。check_phone 参数保留以
    兼容调用方签名，但不再触发必填违规；空值放行，非空格式仍由 field_validator 校验。

    客人价：去掉"挂牌价(list_price)强制"（王总要求，OTA 直连单 list_price 常为空）。
    放宽为"list_price 或 actual_price 至少有其一非空"——前端 normal 单本就强制
    actual_price，手填单仍保证有价；OTA 单靠 actual_price 兜底即可解套。两者皆空才拦。
    """
    if booking_type != BookingType.normal:
        return None
    if check_list_price:
        for r in rooms or []:
            if r.list_price is None and r.actual_price is None:
                return NORMAL_REQUIRES_LIST_PRICE
    return None


# ─── 到手价退化守卫（P3）───────────────────────────────────────────────────────
# 填了 ota_owner_revenue(到手价/净房费) 但没房费(actual_price) 时，
# derive_ota_commission_rate 见 actual_total 为 None/≤0 返回 None，佣金率倒推不出 →
# net_revenue=0 但 expected_revenue 有值，账面对不上。三个录单界面都必送 actual_price，
# UI 触发不到，仅纯 API 直连可踩（故 P3）。创建期在 OrderCreate schema 校验；编辑期是
# 部分更新、schema 拿不到订单原值，改在 endpoint(update_order) 按合并态判更准。
OWNER_REVENUE_REQUIRES_ACTUAL = "填了到手价必须同时提供房费(actual_price)"


def owner_revenue_requires_actual_violation(
    ota_owner_revenue: Optional[Decimal],
    actual_price: Optional[Decimal],
    rooms,
) -> Optional[str]:
    """返回违规文案，合规返回 None。ota_owner_revenue 为空一律豁免。

    房费认顶层 actual_price 或 rooms 的房价合计(sum of 各房 actual_price)，任一 > 0 即放行。
    rooms 元素 duck-typed（schema 的 OrderRoomCreate 与 ORM 的 OrderRoom 皆有 .actual_price）。
    """
    if ota_owner_revenue is None:
        return None
    total = actual_price
    if total is None or total <= 0:
        room_prices = [r.actual_price for r in (rooms or []) if r.actual_price is not None]
        total = sum(room_prices) if room_prices else None
    if total is None or total <= 0:
        return OWNER_REVENUE_REQUIRES_ACTUAL
    return None


# ─── 每房净房费（B 方案，2026-07-04 拍板）────────────────────────────────────────
# 多房平台单每间房手填净房费，落 OrderRoom.metadata["ota_owner_revenue"]。
# 整单 Order.metadata.ota_owner_revenue 仍是佣金率倒推唯一入口 = Σ每房净，
# 不引入第二真相源。铁律：任一行填了 ⇒ 全填 + 每房必须有房费 + Σ=整单(±1分)。
PER_ROOM_OWNER_REVENUE_PARTIAL = "每房净房费要么全填要么全不填（缺的房间也要填）"
PER_ROOM_OWNER_REVENUE_REQUIRES_PRICE = "填了每房净房费的订单，每间房都必须有房费(actual_price)"
PER_ROOM_OWNER_REVENUE_SUM_MISMATCH = "每房净房费之和必须等于整单净房费"

_SUM_TOLERANCE = Decimal("0.01")


def per_room_owner_revenue_violation(rooms, order_owner_revenue: Optional[Decimal]) -> Optional[str]:
    """校验每房净房费口径。返回违规文案，合规（含全不填）返回 None。

    rooms 元素 duck-typed：需有 .ota_owner_revenue / .actual_price。
    """
    nets = [getattr(r, "ota_owner_revenue", None) for r in (rooms or [])]
    filled = [n for n in nets if n is not None]
    if not filled:
        return None
    if len(filled) != len(nets):
        return PER_ROOM_OWNER_REVENUE_PARTIAL
    if any(r.actual_price is None for r in rooms):
        return PER_ROOM_OWNER_REVENUE_REQUIRES_PRICE
    # ±1 分容差（差 >1 分才算不一致），与文档「Σ=整单(±1分)」口径一致。
    if order_owner_revenue is not None and abs(sum(filled) - order_owner_revenue) > _SUM_TOLERANCE:
        return PER_ROOM_OWNER_REVENUE_SUM_MISMATCH
    return None


# ─── Multi-room schemas (多房订单)──────────────────────────────────────────────

class OrderRoomCreate(BaseModel):
    """一条房间行（新建/整体替换时使用）。order_room_id 由后端生成。"""
    room_id: Optional[str] = None  # null = 待排房
    check_in_date: date
    check_out_date: date
    list_price: Optional[Decimal] = None
    discount_amount: Decimal = Decimal("0")
    actual_price: Optional[Decimal] = None
    guests_count: int = 0
    position: int = 0
    # 每房净房费（平台结给我们，多房平台单手填）。落 OrderRoom.metadata，
    # 口径闸见 per_room_owner_revenue_violation。
    ota_owner_revenue: Optional[Decimal] = None

    @field_validator("check_out_date")
    @classmethod
    def _checkout_after_checkin(cls, v, info):
        if "check_in_date" in info.data and v <= info.data["check_in_date"]:
            raise ValueError("退房日期必须晚于入住日期")
        return v

    @field_validator("ota_owner_revenue")
    @classmethod
    def _owner_revenue_valid(cls, v):
        return _non_negative(v, field="ota_owner_revenue")

    @field_validator("list_price")
    @classmethod
    def _list_price_valid(cls, v):
        return _non_negative(v, field="list_price")

    @field_validator("actual_price")
    @classmethod
    def _actual_price_valid(cls, v):
        return _non_negative(v, field="actual_price")

    @field_validator("discount_amount")
    @classmethod
    def _discount_valid(cls, v):
        return _non_negative(v, field="discount_amount")

    @field_validator("guests_count")
    @classmethod
    def _guests_non_negative(cls, v):
        if v < 0:
            raise ValueError("guests_count 不能为负数")
        return v

    @property
    def nights(self) -> int:
        return (self.check_out_date - self.check_in_date).days


class OrderRoomOut(BaseModel):
    order_room_id: str
    room_id: Optional[str]
    check_in_date: date
    check_out_date: date
    nights: int
    list_price: Optional[Decimal]
    discount_amount: Decimal
    actual_price: Optional[Decimal]
    guests_count: int
    position: int
    # 单间退房标记：NULL=在住/未退，非空=该房已退房时刻。前端据此在多房单里只列在住房、
    # 判断是否整单收尾。
    checked_out_at: Optional[datetime] = None
    # 单间入住标记：NULL=未入住，非空=该房已入住时刻。前端多房单据此在每房显示「已入住/待入住」、
    # 决定是否给该房显示「办理入住」按钮。存量单（本列上线前已入住）为 NULL，前端遇 NULL 时
    # 按订单 order_status 兜底（已 checked_in 及之后视为已入住）。
    checked_in_at: Optional[datetime] = None
    # 每房净房费（手填才有值；来自 OrderRoom.metadata，ORM property 透出）。
    ota_owner_revenue: Optional[Decimal] = None
    # 每日房价快照 { "YYYY-MM-DD": "253.38" }。前端按入住区间日期顺序遍历显示。
    daily_prices: dict[str, Decimal] = {}
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DailyPriceUpdate(BaseModel):
    """单日改价请求体。"""
    date: date
    price: Decimal

    @field_validator("price")
    @classmethod
    def _price_valid(cls, v):
        if v < 0:
            raise ValueError("单日房价不能为负数")
        if v > _MAX_PRICE:
            raise ValueError("单日房价超过上限")
        return v


class OrderCreate(BaseModel):
    channel: Channel
    platform_order_id: Optional[str] = None
    guest_name: str
    # issue#3: 手机号选填。前期订单来时常无客人电话，前台先录单再补。
    # 非空时仍校验 11 位中国大陆手机号格式（避免乱填）。
    guest_phone: Optional[str] = None
    # issue#6: 订单类型，默认普通单。试住/自住单业主出钱，按 Room.share_ratio_* 扣分成
    booking_type: BookingType = BookingType.normal
    # Multi-room (多房订单): 新客户端走这里。可选；为空时按下方旧字段构造单房 rooms[0]
    rooms: Optional[list[OrderRoomCreate]] = None
    # DEPRECATED：旧字段，为保留向后兼容仍接受。新代码请用 rooms 数组。
    # 顶层 check_in_date/check_out_date 在多房场景下变成"派生"（min/max of rooms）。
    room_id: Optional[str] = None
    check_in_date: Optional[date] = None
    check_out_date: Optional[date] = None
    list_price: Optional[Decimal] = None
    discount_amount: Decimal = Decimal("0")
    actual_price: Optional[Decimal] = None
    deposit: Decimal = Decimal("0")
    platform_commission_rate: Decimal = Decimal("0")
    # 平台单「到手价(净房费=平台结给我们)」。手录平台单直接填此值，写入
    # metadata.ota_owner_revenue，佣金率由 sync_ota_commission_rate 自动倒推
    # (net_revenue≡到手价)，取代易错的手填佣金率。非平台/私单留空即可。
    ota_owner_revenue: Optional[Decimal] = None
    notes: Optional[str] = None
    # 前台电话单直达：创建后原子推进状态（确认订单；全排房则再到待入住），
    # 省去列表里两次纯仪式流转。守卫复用 _apply_order_transition。
    auto_confirm: bool = False
    # 同客同日期重复单拦截的显式覆盖开关：默认拦（409 duplicate_order），
    # 前端确认「确为同名不同客」后带 true 重发。
    allow_duplicate: bool = False

    @field_validator("guest_name")
    @classmethod
    def _trim_guest_name(cls, v):
        # 姓名统一去首尾空白，落库与去重口径一致（批2 item4）；去空后为空则拒。
        if v is None:
            return v
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("客人姓名不能为空")
        return trimmed

    @field_validator("check_out_date")
    @classmethod
    def checkout_after_checkin(cls, v, info):
        if v is None:
            return v
        ci = info.data.get("check_in_date")
        if ci and v <= ci:
            raise ValueError("退房日期必须晚于入住日期")
        return v

    @field_validator("guest_phone")
    @classmethod
    def _phone_format_when_present(cls, v):
        return _validate_phone_format(v)

    @field_validator("list_price")
    @classmethod
    def _list_price_valid(cls, v):
        return _non_negative(v, field="list_price")

    @field_validator("actual_price")
    @classmethod
    def _actual_price_valid(cls, v):
        return _non_negative(v, field="actual_price")

    @field_validator("discount_amount")
    @classmethod
    def _discount_valid(cls, v):
        return _non_negative(v, field="discount_amount")

    @field_validator("deposit")
    @classmethod
    def _deposit_valid(cls, v):
        return _non_negative(v, field="deposit")

    @field_validator("platform_commission_rate")
    @classmethod
    def _commission_valid(cls, v):
        return _validate_commission_rate(v)

    @field_validator("ota_owner_revenue")
    @classmethod
    def _owner_revenue_valid(cls, v):
        return _non_negative(v, field="ota_owner_revenue")

    @model_validator(mode="after")
    def _cross_field_checks(self):
        # issue#6: 试住/自住单业主出钱，可能实收 0，跳过押金>实收价校验
        if self.booking_type == BookingType.normal:
            if self.actual_price is not None and self.deposit > self.actual_price:
                raise ValueError("押金不能超过实收价")
        return self

    @model_validator(mode="after")
    def _reconcile_rooms_and_legacy_fields(self):
        """支持旧 payload（只填顶层 room_id/dates）和新 payload（rooms 数组）。

        - 新 payload：从 rooms[0] 回填顶层 room_id/dates/list_price 给阶段 3 之前的
          老 API 代码用；顶层 check_in/out 取 min/max 跨度。
        - 旧 payload：从顶层字段合成 rooms=[单行]，便于阶段 3 后的代码统一走 rooms。
        - 两者都没有：报错（必须至少有一个房间信息）。
        """
        if self.rooms:
            # 新 payload — 回填顶层
            first = self.rooms[0]
            if self.room_id is None:
                self.room_id = first.room_id
            if self.list_price is None:
                self.list_price = first.list_price
            self.check_in_date = min(r.check_in_date for r in self.rooms)
            self.check_out_date = max(r.check_out_date for r in self.rooms)
        else:
            # 旧 payload — 顶层 dates 必填
            if self.check_in_date is None or self.check_out_date is None:
                raise ValueError("必须提供 rooms 数组或顶层 check_in_date/check_out_date")
            if self.check_out_date <= self.check_in_date:
                raise ValueError("退房日期必须晚于入住日期")
            # 合成单行 rooms
            self.rooms = [OrderRoomCreate(
                room_id=self.room_id,
                check_in_date=self.check_in_date,
                check_out_date=self.check_out_date,
                list_price=self.list_price,
                discount_amount=self.discount_amount,
                actual_price=self.actual_price,
                position=0,
            )]
        return self

    @model_validator(mode="after")
    def _normal_booking_requires_list_price_and_phone(self):
        """手填 normal 单必填客人价（逐房）。客人电话已改为选填（前台先录后补）。

        试住/自住（业主出钱的内部占用）豁免，与 actual_price 校验口径一致。
        OTA 搬单走 ota-sync raw SQL 直写库，不经此 schema，天然不受影响。
        在 _reconcile_rooms_and_legacy_fields 之后执行，确保 self.rooms 已填充。
        """
        msg = normal_booking_violation(self.booking_type, self.guest_phone, self.rooms)
        if msg:
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _owner_revenue_requires_actual_price(self):
        """填了到手价就必须有房费(actual_price 或 rooms 房价合计)，否则佣金率倒推不出。

        在 _reconcile_rooms_and_legacy_fields 之后执行，确保 self.rooms 已填充。
        """
        msg = owner_revenue_requires_actual_violation(
            self.ota_owner_revenue, self.actual_price, self.rooms
        )
        if msg:
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _per_room_owner_revenue_caliber(self):
        """每房净房费口径闸；全填且整单未给时整单自动 = Σ（保持倒推入口唯一）。

        在 _reconcile_rooms_and_legacy_fields 之后执行，确保 self.rooms 已填充。
        """
        msg = per_room_owner_revenue_violation(self.rooms, self.ota_owner_revenue)
        if msg:
            raise ValueError(msg)
        if self.ota_owner_revenue is None and self.rooms:
            nets = [r.ota_owner_revenue for r in self.rooms]
            if nets and all(n is not None for n in nets):
                self.ota_owner_revenue = sum(nets)
        return self


class OrderUpdate(BaseModel):
    channel: Optional[Channel] = None
    guest_name: Optional[str] = None
    guest_phone: Optional[str] = None
    booking_type: Optional[BookingType] = None  # issue#6
    # Multi-room: 整体替换。None = 不动 rooms；[] 不允许（必须至少一行）
    rooms: Optional[list[OrderRoomCreate]] = None
    room_id: Optional[str] = None  # DEPRECATED
    check_in_date: Optional[date] = None
    check_out_date: Optional[date] = None
    list_price: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    actual_price: Optional[Decimal] = None
    deposit: Optional[Decimal] = None
    deposit_status: Optional[DepositStatus] = None
    order_status: Optional[OrderStatus] = None
    payment_status: Optional[PaymentStatus] = None
    platform_commission_rate: Optional[Decimal] = None
    # 平台单「到手价(净房费)」，语义同 OrderCreate.ota_owner_revenue。编辑期可补录/更正，
    # 写入 metadata.ota_owner_revenue 后由 sync_ota_commission_rate 倒推佣金率。
    ota_owner_revenue: Optional[Decimal] = None
    notes: Optional[str] = None
    # 把入住日改到过去的显式确认（同 OrderCreate.allow_duplicate 惯例）：守卫默认拦
    # 「把活单推到过去」防手滑，前端弹窗确认后带 true 重发放行——纠错(客人其实上周就来了)
    # 与手滑是同一个动作，机器分不出，只能问人。非列，不入库。
    allow_past_dates: bool = False

    @field_validator("guest_name")
    @classmethod
    def _trim_guest_name(cls, v):
        # 编辑期同样 trim（批2 item4）：与创建期一致，避免 PATCH 带空白名绕过。
        if v is None:
            return v
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("客人姓名不能为空")
        return trimmed

    @field_validator("guest_phone")
    @classmethod
    def _phone_format_when_present(cls, v):
        # issue#103 Step3 adversarial: 编辑期也校验手机号格式，与创建期一致，
        # 否则可 PATCH 一个畸形手机号到 normal 单(创建期挡得住,编辑期漏)。
        return _validate_phone_format(v)

    @field_validator("list_price")
    @classmethod
    def _list_price_valid(cls, v):
        return _non_negative(v, field="list_price")

    @field_validator("actual_price")
    @classmethod
    def _actual_price_valid(cls, v):
        return _non_negative(v, field="actual_price")

    @field_validator("discount_amount")
    @classmethod
    def _discount_valid(cls, v):
        return _non_negative(v, field="discount_amount")

    @field_validator("deposit")
    @classmethod
    def _deposit_valid(cls, v):
        return _non_negative(v, field="deposit")

    @field_validator("platform_commission_rate")
    @classmethod
    def _commission_valid(cls, v):
        return _validate_commission_rate(v)

    @field_validator("ota_owner_revenue")
    @classmethod
    def _owner_revenue_valid(cls, v):
        return _non_negative(v, field="ota_owner_revenue")

    @model_validator(mode="after")
    def _checkout_after_checkin(self):
        # 仅在两个字段都提供时校验。单字段更新时由 endpoint 用合并后的日期再判。
        if self.check_in_date and self.check_out_date and self.check_out_date <= self.check_in_date:
            raise ValueError("退房日期必须晚于入住日期")
        # rooms 显式传 [] 不合法（必须 None 表示不动，或至少 1 行）
        if self.rooms is not None and len(self.rooms) == 0:
            raise ValueError("rooms 不能为空数组；若不修改房间请省略该字段")
        # 新 payload 提供 rooms 时，自动派生顶层 check_in/out（覆盖任何顶层旧值）
        if self.rooms:
            self.check_in_date = min(r.check_in_date for r in self.rooms)
            self.check_out_date = max(r.check_out_date for r in self.rooms)
            if self.room_id is None:
                self.room_id = self.rooms[0].room_id
            # 每房净房费口径闸（同 OrderCreate）：整体替换语义下 rooms 即全量，
            # 全填且整单未给时整单自动 = Σ。
            msg = per_room_owner_revenue_violation(self.rooms, self.ota_owner_revenue)
            if msg:
                raise ValueError(msg)
            if self.ota_owner_revenue is None:
                nets = [r.ota_owner_revenue for r in self.rooms]
                if nets and all(n is not None for n in nets):
                    self.ota_owner_revenue = sum(nets)
        return self


class ManualOverrideUpdate(BaseModel):
    action: Literal["unlock"]
    fields: list[OrderSyncField] = Field(min_length=1)
    reason: str = Field(min_length=2, max_length=200)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class SplitSegment(BaseModel):
    check_in_date: date
    check_out_date: date
    room_id: str = Field(min_length=1, max_length=10)
    settlement_kind: StaySettlementKind

    model_config = {"extra": "forbid"}


class ZeroFeeSplitRequest(BaseModel):
    expected_group_version: int = Field(ge=0)
    price_snapshot_id: str = Field(min_length=1, max_length=40)
    segments: list[SplitSegment] = Field(min_length=1)

    model_config = {"extra": "forbid"}


class ZeroFeeSplitCompanySponsoredOut(BaseModel):
    calculation_base: Decimal
    settlement_ratio: Decimal
    amount: Decimal


class ZeroFeeSplitSegmentOut(BaseModel):
    order_id: Optional[str] = None
    check_in_date: date
    check_out_date: date
    room_id: str
    settlement_kind: StaySettlementKind
    company_sponsored: Optional[ZeroFeeSplitCompanySponsoredOut] = None


class ZeroFeeSplitOut(BaseModel):
    stay_group_id: Optional[str] = None
    group_version: int
    segments: list[ZeroFeeSplitSegmentOut]


class ManualControlSplitOut(BaseModel):
    enabled: bool
    visible: bool
    eligible: bool
    blocker_code: Optional[str] = None
    blocker_message: Optional[str] = None
    group_version: int = 0


class SourcePriceSnapshotOut(BaseModel):
    source_price_snapshot_id: str
    version: int
    channel: Channel
    check_in_date: date
    check_out_date: date
    nightly_bases: dict[str, str]
    total: Decimal
    origin: str

    model_config = {"from_attributes": True, "frozen": True}


class SourcePriceSnapshotAdminOverrideRequest(BaseModel):
    based_on_snapshot_id: Optional[str] = Field(default=None, max_length=40)
    nightly_prices: Optional[dict[str, Decimal]] = None
    total: Optional[Decimal] = None
    reason: str = Field(min_length=2, max_length=200)

    model_config = {"extra": "forbid"}

    @field_validator("nightly_prices", mode="before")
    @classmethod
    def require_nightly_decimal_strings(cls, value):
        if value is None:
            return value
        if not isinstance(value, dict) or not all(
            isinstance(item, str) for item in value.values()
        ):
            raise ValueError("money must be decimal strings")
        return value

    @field_validator("total", mode="before")
    @classmethod
    def require_total_decimal_string(cls, value):
        if value is not None and not isinstance(value, str):
            raise ValueError("money must be a decimal string")
        return value

    @field_validator("total")
    @classmethod
    def bound_total(cls, value):
        return _non_negative(value, field="来源总价")

    @field_validator("nightly_prices")
    @classmethod
    def bound_nightly_prices(cls, value):
        if value is None:
            return value
        bounded = {
            stay_date: _non_negative(amount, field=f"{stay_date} 来源价格")
            for stay_date, amount in value.items()
        }
        if sum(bounded.values(), Decimal("0")) > _MAX_PRICE:
            raise ValueError("来源总价超过上限")
        return bounded

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_exactly_one_price_shape(self):
        if (self.nightly_prices is None) == (self.total is None):
            raise ValueError("provide exactly one of nightly_prices or total")
        return self


class ManualControlLockedFieldOut(BaseModel):
    field: str
    current_value: object


class ManualControlConflictOut(BaseModel):
    conflict_id: str
    field: str
    local_value: object
    upstream_value: object
    upstream_version: str
    can_restore_following: bool
    first_seen_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True, "frozen": True}


class OrderManualControlOut(BaseModel):
    source_order_id: str
    is_relevant: bool
    split: ManualControlSplitOut
    source_price_snapshot: Optional[SourcePriceSnapshotOut] = None
    locked_fields: list[ManualControlLockedFieldOut] = []
    open_conflicts: list[ManualControlConflictOut] = []
    can_administer: bool
    can_write: bool


class SyncConflictDecisionRequest(BaseModel):
    action: Literal["preserve", "ignore"]
    reason: str = Field(min_length=2, max_length=200)

    model_config = {"extra": "forbid"}

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class SyncConflictDecisionOut(BaseModel):
    conflict_id: str
    status: str


class CompanySponsorshipAdjustmentOut(BaseModel):
    adjustment_id: str
    correction_of_id: str
    delta: Decimal
    reason: str
    operation_key: str
    actor_id: Optional[str]
    system_principal: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True, "frozen": True}


class CompanySponsoredStayOut(BaseModel):
    sponsored_stay_id: str
    source_order_id: str
    segment_order_id: str
    source_price_snapshot_id: str
    calculation_base: Decimal
    settlement_ratio: Decimal
    amount: Decimal
    version: int
    effective_amount: Decimal
    payment_responsibility: PaymentResponsibility
    status: CompanySponsorshipStatus
    settlement_item_id: Optional[str]
    settlement_batch_id: Optional[str]
    adjustments: list[CompanySponsorshipAdjustmentOut] = []

    model_config = {"from_attributes": True, "frozen": True}


class SponsorshipCorrectionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    delta: Decimal
    reason: str = Field(min_length=2, max_length=200)

    model_config = {"extra": "forbid"}

    @field_validator("delta", mode="before")
    @classmethod
    def require_decimal_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("delta must be a decimal string")
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class CompanySponsorshipOriginalOut(BaseModel):
    sponsored_stay_id: str
    source_order_id: str
    segment_order_id: str
    source_price_snapshot_id: str
    calculation_base: Decimal
    settlement_ratio: Decimal
    amount: Decimal
    payment_responsibility: PaymentResponsibility
    status: CompanySponsorshipStatus

    model_config = {"from_attributes": True, "frozen": True}


class SponsorshipCorrectionResponse(BaseModel):
    original: CompanySponsorshipOriginalOut
    corrections: list[CompanySponsorshipAdjustmentOut]
    current_effective_amount: Decimal
    version: int


class OrderOut(BaseModel):
    order_id: str
    channel: Channel
    platform_order_id: Optional[str]
    guest_name: str
    guest_phone: Optional[str]  # issue#3: 老订单可能仍有值，新订单可空
    booking_type: BookingType = BookingType.normal  # issue#6
    stay_settlement_kind: Optional[StaySettlementKind] = None
    # DEPRECATED：单房语义字段。多房订单的真实数据请用 rooms 数组。
    # 顶层 room_id/check_in_date/check_out_date 在多房订单中是"首房"快照。
    room_id: Optional[str]
    check_in_date: date
    check_out_date: date
    nights: int
    list_price: Optional[Decimal]
    discount_amount: Decimal
    actual_price: Optional[Decimal]
    deposit: Decimal
    deposit_status: DepositStatus
    deposit_returned: Optional[Decimal] = None
    payment_status: PaymentStatus
    order_status: OrderStatus
    cleaning_status: CleaningStatus
    platform_commission_rate: Decimal
    platform_commission: Decimal
    net_revenue: Decimal
    expected_revenue: Optional[Decimal] = None  # 携程预计收入(业主到手),仅OTA搬单单非空
    price_pending: bool = False  # OTA占位价待回填,前端显示"价格同步中"而非¥0
    is_ota_free_room: bool = False
    manual_override_fields: list[str] = Field(default_factory=list)
    is_manually_managed: bool = False
    stay_group_id: Optional[str] = None  # 续住关联组号；同组多张单为一段连续入住
    notes: Optional[str]
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime
    # Multi-room: 嵌套房间数组（按 position 升序）。endpoint 必须 selectinload(Order.rooms)。
    rooms: list[OrderRoomOut] = []
    company_sponsorship: Optional[CompanySponsoredStayOut] = None

    model_config = {"from_attributes": True}


class OrderListItem(BaseModel):
    order_id: str
    channel: Channel
    guest_name: str
    guest_phone: Optional[str]  # issue#3
    booking_type: BookingType = BookingType.normal  # issue#6
    stay_settlement_kind: Optional[StaySettlementKind] = None
    room_id: Optional[str]  # DEPRECATED — 多房订单展示用 room_ids
    check_in_date: date
    check_out_date: date
    nights: int
    actual_price: Optional[Decimal]
    expected_revenue: Optional[Decimal] = None  # 携程预计收入(业主到手),仅OTA搬单单非空
    price_pending: bool = False  # OTA占位价待回填,前端显示"价格同步中"而非¥0
    is_ota_free_room: bool = False
    order_status: OrderStatus
    payment_status: PaymentStatus
    cleaning_status: CleaningStatus
    created_at: datetime
    # Multi-room: 该订单所有 OrderRoom 的 room_id 列表（不含 NULL）。前端可显示
    # "1401, 1402 (+1)"。endpoint 必须 selectinload(Order.rooms) 才能填充。
    room_ids: list[str] = []

    model_config = {"from_attributes": True}


class PaginatedOrderList(BaseModel):
    items: list[OrderListItem]
    total: int
    page: int
    page_size: int


# ─── 续住组整段视图 ───────────────────────────────────────────────────────────
class LinkCandidateOut(BaseModel):
    """续住候选：同房+同名+退房日紧接入住日+未取消。口径来自
    checkout_continuation.find_checkout_continuations，前端不重算。

    ⚠️ 字段必须与该函数的返回字典**逐键对齐**（checkout_continuation.py 的 out.append）：
    它只给 {room_id, next_order_id, guest_name, check_in_date}。**没有 check_out_date**
    ——加字段前先打开那个文件确认它真的返回。
    """
    room_id: str
    next_order_id: str
    guest_name: str
    check_in_date: date


class StaySegmentDetail(BaseModel):
    """段级摘要：房号列表来自 order_rooms（orders.room_id 已 DEPRECATED 不可靠），
    押金字段供前端段列表直显，免得再去 segments 的 OrderOut 里拆嵌套。"""
    order_id: str
    rooms: list[str] = []
    deposit: Decimal = Decimal("0")
    deposit_status: DepositStatus


class StayGroupOut(BaseModel):
    """续住组整段视图：前端据此把一组单渲染成「一段连续入住」。

    segments 保留每段完整 OrderOut（渠道/佣金/平台单号各段独立，分账口径不变）。
    非续住单返回单段退化组（stay_group_id=None），前端不必分叉。
    link_candidates 仅在无组且认出续住时非空。
    """
    stay_group_id: Optional[str] = None
    group_kind: Optional[str] = None            # managed_split | None (ordinary continuation)
    anchor_order_id: str                      # 首段：持门锁码
    last_order_id: str                        # 末段：唯一，退房在这段办
    check_in_date: date
    check_out_date: date
    nights: int
    total_amount: Decimal
    group_status: OrderStatus
    channels: list[str] = []                  # 去重后的渠道，只含活段，按段序
    rooms: list[str] = []                     # 按夜去重的房间序列，跨房时多于一个
    free_room_kind: str = "none"              # none | all | mixed；后端完整组统一计算
    deposit: Decimal = Decimal("0")           # 组内实收那份（非锚单口径，见 group_view）
    deposit_status: DepositStatus
    deposit_order_id: Optional[str] = None    # 押金实际挂在哪段
    deposit_returned: Optional[Decimal] = None  # 持押金段的实退金额（未退为 None）
    segments: list[OrderOut] = []
    segment_details: list[StaySegmentDetail] = []  # 与 segments 同序，含已取消段
    link_candidates: list[LinkCandidateOut] = []


class PaginatedSegmentList(BaseModel):
    """按段分页的响应：一行一段，每行是 StayGroupOut。

    刻意不复用 PaginatedOrderList —— 那个的 items 是 OrderListItem（按单），
    两种语义共用一个容器只会让调用方分不清手里是单还是段。
    total 是段数：一个续住组算 1，一张无组单也算 1。
    """
    items: list[StayGroupOut] = []
    total: int
    page: int
    page_size: int


# ─── 房间转移（换房）─────────────────────────────────────────────────────────
class TransferReason(str, enum.Enum):
    free_upgrade = "free_upgrade"
    room_defect = "room_defect"
    guest_request = "guest_request"
    swap = "swap"
    other = "other"


class OldRoomDisposition(str, enum.Enum):
    maintenance = "maintenance"
    pending_clean = "pending_clean"
    available = "available"


class TransferRoomRequest(BaseModel):
    order_room_id: str
    new_room_id: str
    reason: TransferReason
    transfer_date: date | None = None
    markup_amount: Decimal = Field(default=Decimal("0"), ge=0)
    old_room_disposition: OldRoomDisposition | None = None


class SwapRoomsRequest(BaseModel):
    order_a_id: str
    order_room_a_id: str
    order_b_id: str
    order_room_b_id: str
