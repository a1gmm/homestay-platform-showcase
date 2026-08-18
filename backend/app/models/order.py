from sqlalchemy import (
    String, Date, Numeric, Boolean, Text,
    Enum as PgEnum, DateTime, ForeignKey, func, Integer, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import enum

from app.core.database import Base


class Channel(str, enum.Enum):
    # 客户拍板的 11 个 active 渠道（2026-05-23 王总团队反馈 + 2026-05-25 新增 trial_stay）。
    # 前端下拉只暴露这 11 个。
    ctrip = "ctrip"
    meituan_hotel = "meituan_hotel"
    meituan_homestay = "meituan_homestay"
    douyin = "douyin"
    qunar = "qunar"
    zhixing = "zhixing"
    tongcheng = "tongcheng"
    self_acquired = "self_acquired"   # 自来客（私域复购 / C 端自助 / 老客）
    offline = "offline"                # 线下渠道（旅行社 / 散客上门）
    self_used = "self_used"            # 自住（内部员工占用）
    trial_stay = "trial_stay"          # 试住（潜客体验房，与 self_used 不同语义）
    fliggy = "fliggy"                  # 飞猪（阿里系 OTA 平台，2026-06-12 王总新增）
    # 历史保留值 — PG ENUM 不能删值，前端下拉隐藏，仅供老订单展示
    meituan = "meituan"      # 历史"美团"（未拆分酒店/民宿前的老数据，运营手动改判）
    tujia = "tujia"          # 历史"途家"（客户已弃用此渠道）
    private = "private"      # 历史"私域"（自动迁移到 self_acquired）
    walk_in = "walk_in"      # 历史"散客上门"（自动迁移到 offline）
    direct = "direct"        # 历史"C 端自助下单"（自动迁移到 self_acquired）


# OTA 平台渠道（含历史值）。渠道从这个集合改出去 = 平台单被前台「接手转线下」，
# 订单价格从此以运营为准（update_order 会清冻结的平台定价残留）。
# 与 reconciliation._OTA_CHANNELS（异常扫描用，故意不含已弃用渠道）口径不同，勿合并。
OTA_PLATFORM_CHANNELS = frozenset({
    Channel.ctrip, Channel.meituan_hotel, Channel.meituan_homestay, Channel.douyin,
    Channel.qunar, Channel.zhixing, Channel.tongcheng, Channel.fliggy,
    Channel.meituan, Channel.tujia,
})


class DepositStatus(str, enum.Enum):
    not_collected = "not_collected"
    collected = "collected"
    returned = "returned"
    withheld = "withheld"


class PaymentStatus(str, enum.Enum):
    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"
    partial_refund = "partial_refund"
    full_refund = "full_refund"


class OrderStatus(str, enum.Enum):
    pending_confirm = "pending_confirm"
    pending_payment = "pending_payment"
    paid_pending_room = "paid_pending_room"
    roomed_pending_checkin = "roomed_pending_checkin"
    checked_in = "checked_in"
    pending_checkout = "pending_checkout"
    completed = "completed"
    rescheduled = "rescheduled"
    abnormal = "abnormal"
    cancelled = "cancelled"


# 订单状态 → 中文 label 的单一真相源。给用户看的报错/导出必须经此映射，
# 不能直接暴露原始枚举码（roomed_pending_checkin 这类英文会被当成"英文报错页"）。
# 文案口径与 export.py STATUS_LABELS / 前端 StatusBadge 保持一致（2026-06-05 流程调整版）。
ORDER_STATUS_LABELS: dict[str, str] = {
    "pending_confirm": "待确认",
    "paid_pending_room": "已付待排房",
    "roomed_pending_checkin": "待入住",
    "checked_in": "入住中",
    "pending_checkout": "已退房待收款",
    "pending_payment": "待完成",
    "completed": "已完成",
    "rescheduled": "已改期",
    "abnormal": "异常",
    "cancelled": "已取消",
}


def order_status_label(status: "OrderStatus | str") -> str:
    """把订单状态枚举/字符串转成中文 label；未知值原样返回（防御性兜底）。"""
    key = status.value if isinstance(status, OrderStatus) else str(status)
    return ORDER_STATUS_LABELS.get(key, key)


class CleaningStatus(str, enum.Enum):
    not_assigned = "not_assigned"
    assigned = "assigned"
    in_progress = "in_progress"
    done = "done"
    inspected = "inspected"


# issue#6: 订单类型 — 王总反馈"小程序里会分三个模块组成，普通订单，自住单，试住单"。
# 普通单走平台分成；试住单 = 业主每月免费试住一晚（业主出钱，品质抽查）；
# 自住单 = 业主自己来住（业主出钱）。试住/自住单的业主承担金额按 Room.share_ratio_*
# 字段计算（详见 Phase 2 的 settlement 任务）。
class BookingType(str, enum.Enum):
    normal = "normal"
    trial = "trial"
    owner_self = "owner_self"


class StaySettlementKind(StrEnum):
    """Typed settlement classification for managed split-flow child segments.

    Ordinary orders deliberately store ``NULL`` rather than a third catch-all value.
    """

    free_room = "free_room"
    company_sponsored = "company_sponsored"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_room_dates", "room_id", "check_in_date", "check_out_date"),
        Index("ix_orders_status", "order_status", "is_deleted"),
        Index("ix_orders_checkin", "check_in_date"),
        Index("ix_orders_created", "created_at"),
        Index("ix_orders_stay_group", "stay_group_id"),
    )

    order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    channel: Mapped[Channel] = mapped_column(PgEnum(Channel, name="channel"))
    platform_order_id: Mapped[str | None] = mapped_column(String(100))
    guest_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # issue#3: 手机号改为 nullable — 王总反馈"前期订单来时往往没客人电话，
    # 前台为了能录单只能瞎填"。先录单，等打完客人电话再补 phone。
    guest_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # DEPRECATED：多房订单改用 order_rooms 子表（详见 models/order_room.py）。
    # 保留本字段做向后兼容（旧客户端读取 + 6 个月过渡期），新代码请用 order.rooms。
    room_id: Mapped[str | None] = mapped_column(String(10), ForeignKey("rooms.room_id"))
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    # nights computed in application layer (PostgreSQL generated columns need migration)
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    actual_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    deposit: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    # 退房时实际退还给客人的押金额（null = 尚未退押金）。可少于 deposit：
    # 差额 deposit - deposit_returned 即扣款（客人损坏房间物品），原因存
    # metadata_['deposit_withhold_reason']。2026-06-05 王总补充需求。
    deposit_returned: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    deposit_status: Mapped[DepositStatus] = mapped_column(
        PgEnum(DepositStatus, name="deposit_status"), default=DepositStatus.not_collected
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        PgEnum(PaymentStatus, name="payment_status"), default=PaymentStatus.unpaid
    )
    order_status: Mapped[OrderStatus] = mapped_column(
        PgEnum(OrderStatus, name="order_status"), default=OrderStatus.pending_confirm
    )
    # issue#6: 订单类型，默认普通单。试住/自住的财务记账逻辑放 Phase 2 settlement
    booking_type: Mapped[BookingType] = mapped_column(
        PgEnum(BookingType, name="booking_type"),
        default=BookingType.normal,
        server_default="normal",
    )
    cleaning_status: Mapped[CleaningStatus] = mapped_column(
        PgEnum(CleaningStatus, name="cleaning_status"), default=CleaningStatus.not_assigned
    )
    platform_commission_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # 续住关联组号：同一段连续入住的多张订单共享同一个组号（软关联，不合并、不删单）。
    # null = 不在任何续住组。判定/建解关联统一走 services/stay_group.py。
    stay_group_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stay_settlement_kind: Mapped[StaySettlementKind | None] = mapped_column(
        PgEnum(StaySettlementKind, name="stay_settlement_kind"), nullable=True
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Computed properties
    @property
    def nights(self) -> int:
        if self.check_in_date and self.check_out_date:
            return (self.check_out_date - self.check_in_date).days
        return 0

    @property
    def platform_commission(self) -> Decimal:
        # 平台单优先用「原始到手」倒算佣金：佣金 = 实收 − OTA 原始到手（王总 2026-07-14）。
        # 佣金率是由 (实收−到手)/实收 四舍五入成 4 位反推的，再正推会磨损 1 分；直接用
        # 冻结的原始到手(expected_revenue)算，既准又能保证 实收 = 佣金 + 净收入 自洽。
        # 到手>实收 是 OTA 倒贴补贴单(ota_subsidy 另计)，不接管，回退按佣金率。
        exp = self.expected_revenue
        if self.actual_price and exp is not None and Decimal("0") < exp <= self.actual_price:
            return (self.actual_price - exp).quantize(Decimal("0.01"))
        if self.actual_price and self.platform_commission_rate:
            return (self.actual_price * self.platform_commission_rate).quantize(Decimal("0.01"))
        return Decimal("0")

    @property
    def net_revenue(self) -> Decimal:
        # 净收入 = 实收 − 佣金。平台单 platform_commission 已按原始到手倒算，
        # 故此处对 OTA 单自动 ≡ 原始到手(expected_revenue)，与结算/门户口径一致。
        if self.actual_price:
            return (self.actual_price - self.platform_commission).quantize(Decimal("0.01"))
        return Decimal("0")

    @property
    def expected_revenue(self) -> "Decimal | None":
        """OTA 预计收入(业主到手)。只要 metadata 里有 ota_owner_revenue 就返回该
        OTA 原始值——该字段仅由 OTA 导入途径写入(ota-sync 搬单 / bypms-import 补同步
        等),手填私单不会有,故以"字段存在"为判定,不限 source/渠道(覆盖全部历史 OTA 单)。
        取 OTA 原始值不受手改 actual_price 影响(与 net_revenue 的区别)。展示用,不参与结算。"""
        raw = (self.metadata_ or {}).get("ota_owner_revenue")
        if raw in (None, ""):
            return None
        try:
            return Decimal(str(raw)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    @property
    def price_pending(self) -> bool:
        """OTA 直连单价格待回填占位标记（ota-sync 趟1 先占房写 actual_price=0 +
        metadata.price_pending=True，趟2 enrich 补真实价后移除该标记）。为真时
        actual_price/daily_prices 是 0 占位，前端应显示"价格同步中"而非 ¥0。
        兼容 JSONB 存成 bool True 或字符串 "true"。"""
        raw = (self.metadata_ or {}).get("price_pending")
        return raw is True or str(raw).lower() == "true"

    @property
    def manual_override_fields(self) -> list[str]:
        """Read-only canonical field locks, including compatibility flags."""
        from app.services.manual_override import locked_fields

        return sorted(locked_fields(self.metadata_))

    @property
    def is_manually_managed(self) -> bool:
        return bool(self.manual_override_fields)

    @property
    def is_ota_free_room(self) -> bool:
        """Frozen OTA source fact, with a legacy room-type fallback."""
        from app.core.free_room import is_free_room_type

        meta = self.metadata_ or {}
        if "ota_free_room" in meta:
            raw = meta.get("ota_free_room")
            return raw is True or (isinstance(raw, str) and raw.lower() == "true")
        return is_free_room_type(meta.get("ota_room_type"))

    @property
    def room_ids(self) -> list[str]:
        """Multi-room: 已排房的 room_id 列表（不含待排房 NULL 行）。供前端列表展示。
        前置条件：调用者已用 selectinload(Order.rooms) 预加载，否则会触发 async lazy-load。"""
        return [r.room_id for r in self.rooms if r.room_id is not None]

    # Relationships
    room = relationship("Room", back_populates="orders")  # DEPRECATED：用 .rooms
    rooms = relationship(
        "OrderRoom",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderRoom.position",
    )
    creator = relationship("User", foreign_keys=[created_by], back_populates="orders")
    payments = relationship("Payment", back_populates="order")
    refunds = relationship("Refund", back_populates="order")
    tasks = relationship("Task", back_populates="order")
    company_sponsorship = relationship(
        "CompanySponsoredStay",
        foreign_keys="CompanySponsoredStay.segment_order_id",
        back_populates="segment_order",
        uselist=False,
        lazy="selectin",
    )
